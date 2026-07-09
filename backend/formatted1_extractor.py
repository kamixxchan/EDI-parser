import re
from typing import Any

import fitz  # PyMuPDF
import pandas as pd


FORMATTED1_AUDIT_COLUMNS = [
    "source_page",
    "segment_id",
    "segment_name",
    "needs_review",
    "review_reason",
]

# Segment-level metadata columns. These come from the segment metadata box
# above the Element Summary table (e.g. "Loop: N1 Elements: 4"), not from
# the table itself, so they aren't part of the user-specified table columns
# — they're always appended after them, same as before.
FORMATTED1_METADATA_COLUMNS = ["Loop", "Level"]

STOP_SECTION_PREFIXES = (
    "Syntax Rules:",
    "Semantics:",
    "Comments:",
    "Notes:",
    "Example:",
    "Examples:",
    "Change History:",
    "Loop Summary:",
    "Segment Use:",
)

def append_multiline_value(existing_value: str, new_value: str) -> str:
    """
    Append text to an existing cell using a newline.
    """

    existing_value = str(existing_value or "").strip()
    new_value = str(new_value or "").strip()

    if existing_value and new_value:
        return existing_value + "\n" + new_value

    if existing_value:
        return existing_value

    return new_value


def attach_orphan_continuation_to_previous_record(
    records: list[dict[str, Any]],
    orphan_lines: list[str],
    active_segment_metadata: dict[str, str] | None,
    column_config: "ColumnConfig",
) -> None:
    """
    Attach continuation content at the top of a new page to the previous element.

    Example:
        Previous page ends with:
            TD512 Service Level Code

        Next page starts with:
            Code Name
            SD Saturday
            SE Second Day
            SG Standard Ground
            ST Standard Class

        Then this function attaches those qualifiers to TD512.
    """

    if not records:
        return

    if active_segment_metadata is None:
        return

    if not orphan_lines:
        return

    previous_record = records[-1]

    if previous_record.get("segment_id") != active_segment_metadata.get("segment_id"):
        return

    orphan_qualifiers = extract_qualifiers(orphan_lines, column_config)
    orphan_details = extract_qualifier_details(orphan_lines, column_config)

    if orphan_qualifiers:
        previous_record["Qualifiers"] = append_multiline_value(
            previous_record.get("Qualifiers", ""),
            orphan_qualifiers,
        )

    if orphan_details:
        previous_record["Details"] = append_multiline_value(
            previous_record.get("Details", ""),
            orphan_details,
        )

    refresh_record_review_status(previous_record)



def extract_formatted1_tables_from_pdf(
    pdf_path: str,
    page_start: int,
    page_end: int,
    expected_columns: list[str],
    key_column: str | None = None,
    multiline_column: str | None = None,
    include_audit_columns: bool = True,
) -> pd.DataFrame:
    """
    Extract element-level data from 'Formatted 1' EDI implementation guide PDFs.

    Table columns, the key column, and the multi-line column are all
    supplied by the caller (same shape as the Formatted 2 and Other
    routes) rather than assumed to always be exactly Ref/Id/Element
    Name/Req/Type/Min-Max/Usage — a document with an extra column (e.g.
    "Rep"), a missing one (e.g. no "Id"), or different column names
    still parses correctly as long as the columns given here match what
    the PDF actually shows.

    New logic:
    - Parse normal segment pages using Element Summary.
    - Read Elements count from the top-right metadata box.
    - If the segment continues on the next page, keep parsing until expected count is reached.
    """

    if page_start < 1:
        raise ValueError("page_start must be 1 or greater.")

    if page_end < page_start:
        raise ValueError("page_end must be greater than or equal to page_start.")

    column_config = ColumnConfig(
        expected_columns=expected_columns,
        key_column=key_column,
        multiline_column=multiline_column,
    )

    records: list[dict[str, Any]] = []

    active_segment_metadata: dict[str, str] | None = None
    active_segment_expected_count: int | None = None
    active_segment_extracted_count = 0

    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)

        safe_start = max(1, page_start)
        safe_end = min(page_end, total_pages)

        for page_number in range(safe_start, safe_end + 1):
            page = doc[page_number - 1]
            page_text = page.get_text("text") or ""

            lines = [clean_line(line) for line in page_text.splitlines()]
            lines = [line for line in lines if line]
            lines = preprocess_page_lines(lines)

            if not lines:
                continue

            page_has_element_summary = any(
                line.startswith("Element Summary") for line in lines
            )

            page_records: list[dict[str, Any]] = []

            # ---------------------------------------------------------
            # Case 1: This page starts a new segment Element Summary.
            # ---------------------------------------------------------
            if page_has_element_summary:
                if is_loop_page(lines):
                    # For now, ignore Loop Summary pages as requested.
                    active_segment_metadata = None
                    active_segment_expected_count = None
                    active_segment_extracted_count = 0
                    continue

                metadata = parse_segment_metadata(lines)

                # If no segment ID is detected, do not start a segment.
                if not metadata.get("segment_id"):
                    continue

                active_segment_metadata = metadata
                active_segment_expected_count = get_expected_element_count(metadata)
                active_segment_extracted_count = 0

                element_summary_index = find_element_summary_start(lines)

                if element_summary_index is None:
                    continue

                row_start_index = skip_element_summary_headers(
                    lines=lines,
                    start_index=element_summary_index,
                    column_config=column_config,
                )

                remaining_needed = None

                if active_segment_expected_count is not None:
                    remaining_needed = (
                        active_segment_expected_count
                        - active_segment_extracted_count
                    )

                page_records = parse_element_records_from_lines(
                    lines=lines,
                    start_index=row_start_index,
                    metadata=metadata,
                    page_number=page_number,
                    column_config=column_config,
                    max_records=remaining_needed,
                )

            # ---------------------------------------------------------
            # Case 2: Continuation page for previous segment.
            # ---------------------------------------------------------
            else:
                if active_segment_metadata is None:
                    continue

                if active_segment_expected_count is None:
                    continue

                if active_segment_extracted_count >= active_segment_expected_count:
                    active_segment_metadata = None
                    active_segment_expected_count = None
                    active_segment_extracted_count = 0
                    continue

                first_key_index = find_first_key_line_index(lines, column_config)

                if first_key_index is None:
                    # The page may contain only remaining Code/Name rows for the previous element.
                    attach_orphan_continuation_to_previous_record(
                        records=records,
                        orphan_lines=lines,
                        active_segment_metadata=active_segment_metadata,
                        column_config=column_config,
                    )

                    if (
                        active_segment_expected_count is not None
                        and active_segment_extracted_count >= active_segment_expected_count
                        and not last_record_has_incomplete_qualifiers(
                            records=records,
                            active_segment_metadata=active_segment_metadata,
                        )
                    ):
                        active_segment_metadata = None
                        active_segment_expected_count = None
                        active_segment_extracted_count = 0

                    continue

                # Important:
                # If there is content before the first new element row, it belongs to the
                # previous element from the previous page.
                #
                # Example:
                #   CodeList Summary...
                #   Code Name
                #   EM Electronic Mail
                #   PER08 364 Communication Number ...
                #
                # The Code/Name block belongs to PER07, not PER08.
                if first_key_index > 0:
                    orphan_lines = lines[:first_key_index]

                    attach_orphan_continuation_to_previous_record(
                        records=records,
                        orphan_lines=orphan_lines,
                        active_segment_metadata=active_segment_metadata,
                        column_config=column_config,
                    )

                remaining_needed = (
                    active_segment_expected_count
                    - active_segment_extracted_count
                )

                page_records = parse_element_records_from_lines(
                    lines=lines,
                    start_index=first_key_index,
                    metadata=active_segment_metadata,
                    page_number=page_number,
                    column_config=column_config,
                    max_records=remaining_needed,
                )

            if page_records:
                records.extend(page_records)
                active_segment_extracted_count += len(page_records)

            # If expected count is reached, close active segment.
            if (
                active_segment_expected_count is not None
                and active_segment_extracted_count >= active_segment_expected_count
            ):
                # Do not close the segment if the last element's qualifier table
                # is incomplete. It may continue at the top of the next page.
                if not last_record_has_incomplete_qualifiers(
                    records=records,
                    active_segment_metadata=active_segment_metadata,
                ):
                    active_segment_metadata = None
                    active_segment_expected_count = None
                    active_segment_extracted_count = 0

    if not records:
        output_cols = _resolve_output_columns(
            column_config, include_audit_columns, details_has_notes=False
        )
        return pd.DataFrame(columns=output_cols)

    df = pd.DataFrame(records)

    details_has_notes = (
        "Details" in df.columns
        and df["Details"].astype(str).str.contains(r"\[", regex=True).any()
    )

    output_cols = _resolve_output_columns(
        column_config, include_audit_columns, details_has_notes
    )

    for col in output_cols:
        if col not in df.columns:
            df[col] = ""

    return df[output_cols]


def _resolve_output_columns(
    column_config: "ColumnConfig",
    include_audit_columns: bool,
    details_has_notes: bool,
) -> list[str]:
    """
    Build the final ordered column list:
    audit columns -> user-specified table columns (in the order given,
    with the multi-line column expanded into Element Name/Description/
    Qualifiers/Details in place) -> segment metadata columns (Loop,
    Level).

    'Details' is dropped when no qualifier has bracket notes, so the
    column only shows up when it adds information.
    """

    cols: list[str] = []

    for col in column_config.columns:
        cols.append(col)

        if col == column_config.multiline_column:
            cols.extend(["Description", "Qualifiers", "Details"])

    if not details_has_notes and "Details" in cols:
        cols.remove("Details")

    cols = cols + FORMATTED1_METADATA_COLUMNS

    if include_audit_columns:
        cols = FORMATTED1_AUDIT_COLUMNS + cols

    return cols

def parse_formatted1_page_text(
    page_text: str,
    page_number: int,
    expected_columns: list[str],
    key_column: str | None = None,
    multiline_column: str | None = None,
) -> list[dict[str, Any]]:
    """
    Parse one page of a Formatted 1 document. Kept as a standalone,
    single-page utility for callers that don't need the full multi-page
    PDF pipeline; builds the same column config as
    extract_formatted1_tables_from_pdf and delegates the actual row walk
    to parse_element_records_from_lines.
    """

    column_config = ColumnConfig(
        expected_columns=expected_columns,
        key_column=key_column,
        multiline_column=multiline_column,
    )

    lines = [clean_line(line) for line in page_text.splitlines()]
    lines = [line for line in lines if line]
    lines = preprocess_page_lines(lines)

    if not any(line.startswith("Element Summary") for line in lines):
        return []

    # Ignore pages that describe loops rather than segment elements.
    # Example: "Loop Baseline Item Data"
    if is_loop_page(lines):
        return []

    metadata = parse_segment_metadata(lines)

    element_summary_index = find_element_summary_start(lines)

    if element_summary_index is None:
        return []

    row_start_index = skip_element_summary_headers(
        lines=lines,
        start_index=element_summary_index,
        column_config=column_config,
    )

    return parse_element_records_from_lines(
        lines=lines,
        start_index=row_start_index,
        metadata=metadata,
        page_number=page_number,
        column_config=column_config,
    )


def clean_line(value: Any) -> str:
    """
    Clean one text line from PDF extraction.
    """

    return re.sub(r"\s+", " ", str(value or "")).strip()


_RAW_EDI_SEGMENT_RE = re.compile(r"^[A-Z][A-Z0-9]{1,3}\*\S")


def is_raw_edi_segment_line(line: str) -> bool:
    """
    Detect a literal raw X12 segment string, e.g. "FOB*PP~", "CSH*N", or
    "DTM*063*20090731" (the tilde segment terminator isn't always kept
    by every vendor's guide, so it isn't required) — the kind of line a
    guide includes as a "Data Sample" / "Sample Segment" / usage example
    below the element table. This is shape-based (segment ID followed by
    asterisk-delimited elements — standard X12 syntax) rather than tied
    to any vendor's wording for the section label, so it generalizes
    across guides that call this section "Data Sample:", "Sample Data::",
    "Sample Segment:", "Example:", or anything else.
    """

    return bool(_RAW_EDI_SEGMENT_RE.match(clean_line(line)))


def _collapse_duplicate_line_blocks(
    lines: list[str],
    max_block: int = 12,
) -> list[str]:
    """
    Collapse a block of lines that's immediately followed by an exact
    repeat of itself — a common PDF two-column text-extraction artifact
    where a note, label, or sample line gets read twice in a row:

        ["User Note 1:", "User Note 1:", "You will only receive...",
         "appointment...", "You will only receive...", "appointment..."]
        -> ["User Note 1:", "You will only receive...", "appointment..."]

    Scans left to right; at each position, greedily looks for the
    largest block size (up to ``max_block``) that repeats immediately,
    so a doubled label and a separately-doubled multi-line note body are
    both caught in one pass, regardless of block size.
    """

    n = len(lines)
    result: list[str] = []
    i = 0

    while i < n:
        matched = False
        max_k = min(max_block, (n - i) // 2)

        for k in range(max_k, 0, -1):
            if lines[i:i + k] == lines[i + k:i + 2 * k]:
                result.extend(lines[i:i + k])
                i += 2 * k
                matched = True
                break

        if not matched:
            result.append(lines[i])
            i += 1

    return result


def _strip_sample_blocks(lines: list[str]) -> list[str]:
    """
    Remove illustrative "sample segment" blocks — a label line (however
    it's worded) immediately followed by a raw X12 segment line — along
    with any standalone raw segment line found elsewhere. These appear
    below the element table as usage examples and are never part of the
    table itself.
    """

    n = len(lines)
    result: list[str] = []
    i = 0

    while i < n:
        line = lines[i]

        if is_raw_edi_segment_line(line):
            i += 1
            continue

        if _is_label_only_line(line):
            j = i + 1

            while j < n and not lines[j].strip():
                j += 1

            if j < n and is_raw_edi_segment_line(lines[j]):
                i += 1
                continue

        result.append(line)
        i += 1

    return result


def preprocess_page_lines(lines: list[str]) -> list[str]:
    """
    Normalize a page's raw text lines before any field-level parsing:
    collapse duplicated line blocks, then strip sample-segment blocks
    that live below the table. Order matters — collapsing duplicates
    first means the sample-block stripper only has to recognize a single
    label+segment pair, not a doubled one.
    """

    return _strip_sample_blocks(_collapse_duplicate_line_blocks(lines))


_LABEL_LINE_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9&/.'\-]*\s+){0,4}[A-Z][A-Za-z0-9&/.'\-]*\s*\d{0,2}\s*:+\s*\S"
)


def is_label_line(line: str) -> bool:
    """
    Detect whether a line is a "<Label>: <content>" style line start.

    Shape-based, not word-based: a short run of capitalized tokens
    (1-5 words, optionally ending in a number) immediately followed by a
    colon, with the colon appearing near the start of the line. This
    covers standard generator labels ("Description:") and vendor-specific
    annotation labels ("Adobe:", "Peter Millar Note 1:") equally, without
    hardcoding a list of known labels.
    """

    line = clean_line(line)

    if not line:
        return False

    match = _LABEL_LINE_RE.match(line)

    if not match:
        return False

    colon_index = line.find(":")

    # Keep the colon close to the start of the line so we don't mistake a
    # colon deep inside an ordinary sentence (e.g. "...ratio was 3:2 last
    # year") for a label start.
    return 0 <= colon_index <= 45


def is_generic_note_start(line: str) -> bool:
    """
    Kept for backward compatibility with existing call sites.
    Delegates to the generic, shape-based is_label_line() check.
    """

    return is_label_line(line)


def is_ref_line(line: str) -> bool:
    """
    Detect element reference designator.

    Examples:
    ST01
    BEG03
    N101
    PO106
    FOB01
    """

    line = clean_line(line)

    return bool(re.match(r"^[A-Z]{1,4}[0-9]{1,3}$", line))


def is_id_line(line: str) -> bool:
    """
    Detect element ID number.

    Examples:
    143
    353
    324
    """

    return bool(re.match(r"^\d{1,5}$", clean_line(line)))


def is_req_line(line: str) -> bool:
    """
    Detect requirement code.

    Common values:
    M = Mandatory
    O = Optional
    C = Conditional
    X = Relational / syntax condition
    """

    return clean_line(line) in {"M", "O", "C", "X", "N"}


def is_ref_shape(line: str) -> bool:
    """
    Detect an element reference designator, in either form used across
    vendor guides:
        Compact: ST01, BEG03, N101, PO106, FOB01
        Split:   "BEG 01", "N1 01", "TD5 05" (segment id ends in a digit)
    """

    line = clean_line(line)

    if is_ref_line(line):
        return True

    parts = line.split()

    return bool(
        len(parts) == 2
        and re.match(r"^[A-Z]{1,4}\d?$", parts[0])
        and re.match(r"^\d{2,3}$", parts[1])
    )


def is_type_shape(line: str) -> bool:
    """
    Detect an X12 data-type code: ID, AN, DT, TM, R, N0, R0, and similar.
    """

    return bool(re.match(r"^[A-Z][A-Z0-9]{0,3}$", clean_line(line)))


def is_minmax_shape(line: str) -> bool:
    """
    Detect a Min/Max length value, e.g. "2/2", "1/22", ">1/2".
    """

    return bool(re.match(r"^[><]?\d+\s*/\s*[><]?\d+$", clean_line(line)))


def _looks_like_short_field_value(line: str) -> bool:
    """
    Generic fallback shape for a column whose name doesn't map to one of
    the known roles (ref/id/req/type/min-max) — e.g. Usage, Rep, Level,
    Loop, or any other single-value field a vendor's guide happens to
    use. Real field values here are short (a word or a few words) and
    not ordinary prose, as opposed to description/note text.
    """

    line = clean_line(line)

    if not line:
        return False

    if is_label_line(line):
        return False

    words = line.split()

    return len(words) <= 4 and not _looks_like_prose(line)


def infer_column_role(column_name: str) -> str:
    """
    Infer what kind of value a user-named table column holds, purely
    from the column name's own words — not from a fixed list of known
    column names. This lets any vendor's slightly different column
    labels ("Ref." vs "Reference Designator", "Data Type" vs "Type")
    still map onto the right value shape.
    """

    tokens = set(re.sub(r"[^a-z0-9]+", " ", column_name.strip().lower()).split())

    if not tokens:
        return "generic"

    if tokens & {"ref", "reference", "designator"}:
        return "ref"

    if "id" in tokens:
        return "id"

    if tokens & {"req", "required", "requirement"}:
        return "req"

    if "type" in tokens:
        return "type"

    if tokens & {"min", "max"} or "min/max" in column_name.lower():
        return "minmax"

    return "generic"


def build_column_detector(column_name: str):
    """
    Build a shape-matching function for one user-specified column, based
    on its inferred role. Used to decide, generically, where one
    column's value ends and the next one's begins.
    """

    role = infer_column_role(column_name)

    detectors = {
        "ref": is_ref_shape,
        "id": is_id_line,
        "req": is_req_line,
        "type": is_type_shape,
        "minmax": is_minmax_shape,
    }

    return detectors.get(role, _looks_like_short_field_value)


class ColumnConfig:
    """
    Holds the user-specified table columns for a Formatted 1 document,
    plus the derived key column, multi-line column, and a per-column
    shape detector — replacing the old hardcoded assumption that every
    Formatted 1 document has exactly Ref/Id/Element Name/Req/Type/
    Min/Max/Usage in that order.
    """

    def __init__(
        self,
        expected_columns: list[str],
        key_column: str | None = None,
        multiline_column: str | None = None,
    ):
        self.columns = [c.strip() for c in expected_columns if c and c.strip()]

        if not self.columns:
            raise ValueError("Please provide at least one table column.")

        self.key_column = (
            key_column if key_column in self.columns else self.columns[0]
        )

        self.multiline_column = (
            multiline_column if multiline_column in self.columns else None
        )

        self.key_index = self.columns.index(self.key_column)

        self._detectors = {col: build_column_detector(col) for col in self.columns}

        # The key column is, by definition, what marks a new record —
        # always use the dedicated ref-shape detector for it regardless
        # of its inferred role, since that's the one reliable structural
        # anchor in this document family (an element reference like
        # "BEG01" or "FOB01").
        self._detectors[self.key_column] = is_ref_shape

    def detector_for(self, column_name: str):
        return self._detectors.get(column_name, _looks_like_short_field_value)

    def is_key_line(self, line: str) -> bool:
        return self.detector_for(self.key_column)(line)


def is_stop_section(line: str) -> bool:
    """
    Detect sections after the Element Summary table that should be ignored.
    """

    line = clean_line(line)

    return any(line.startswith(prefix) for prefix in STOP_SECTION_PREFIXES)


def is_loop_page(lines: list[str]) -> bool:
    """
    Ignore pages that start with 'Loop ...' instead of a segment ID.

    Example:
    Loop Baseline Item Data
    Loop Extended Reference Information
    """

    try:
        element_summary_index = next(
            i for i, line in enumerate(lines)
            if line.startswith("Element Summary")
        )
    except StopIteration:
        element_summary_index = min(25, len(lines))

    early_lines = lines[:element_summary_index]

    for line in early_lines:
        if re.match(r"^Loop\b", line) and not line.startswith("Loop:"):
            return True

    return False

def parse_segment_metadata(lines: list[str]) -> dict[str, str]:
    """
    Extract segment-level metadata from the top part of the page.

    Expected Formatted 1 block:

        PER Administrative Communications Contact
        Pos: 360 Max: >1
        Heading - Optional
        Loop: N1 Elements: 8

    We need:
        segment_id
        segment_name
        level
        loop
        elements_expected
    """

    metadata = {
        "segment_id": "",
        "segment_name": "",
        "level": "",
        "loop": "",
        "elements_expected": "",
    }

    pos_index = next(
        (i for i, line in enumerate(lines) if "Pos:" in line),
        None,
    )

    if pos_index is None:
        return metadata

    before_pos = lines[:pos_index]

    segment_candidates = []

    for index, line in enumerate(before_pos):
        line = clean_line(line)

        # Segment IDs are usually ST, BEG, REF, N1, PO1, PID, PER, etc.
        if re.match(r"^[A-Z0-9]{2,4}$", line):
            if not line.lower().startswith("v"):
                segment_candidates.append((index, line))

    if segment_candidates:
        segment_index, segment_id = segment_candidates[-1]
        metadata["segment_id"] = segment_id

        name_lines = []

        for line in before_pos[segment_index + 1:]:
            line = clean_line(line)

            if not line:
                continue

            if "internal use" in line.lower():
                continue

            if re.match(r"^\d+$", line):
                continue

            name_lines.append(line)

        metadata["segment_name"] = " ".join(name_lines).strip()

    # Parse right-side metadata box.
    for index in range(pos_index, min(pos_index + 20, len(lines))):
        line = clean_line(lines[index])

        # Example:
        # Heading - Optional
        # Detail - Mandatory
        # Not Defined - Mandatory
        if " - " in line and not line.startswith(("Pos:", "Max:", "Loop:", "Elements:")):
            metadata["level"] = line.split(" - ", 1)[0].strip()

        # Example:
        # Loop: N1 Elements: 8
        loop_and_elements_match = re.search(
            r"Loop:\s*(.*?)\s+Elements:\s*([0-9]+|N/A)",
            line,
            flags=re.IGNORECASE,
        )

        if loop_and_elements_match:
            metadata["loop"] = loop_and_elements_match.group(1).strip()
            metadata["elements_expected"] = loop_and_elements_match.group(2).strip()
            continue

        # Example:
        # Loop: N/A
        if line.startswith("Loop:"):
            loop_value = line.split(":", 1)[1].strip()
            metadata["loop"] = loop_value

        # Example:
        # Elements: 8
        elements_match = re.search(r"Elements:\s*([0-9]+)", line, flags=re.IGNORECASE)

        if elements_match:
            metadata["elements_expected"] = elements_match.group(1).strip()

    return metadata


def get_expected_element_count(metadata: dict[str, str]) -> int | None:
    """
    Convert metadata['elements_expected'] into integer.

    If Elements is N/A or missing, return None.
    """

    value = str(metadata.get("elements_expected", "")).strip()

    if not value or value.upper() == "N/A":
        return None

    if value.isdigit():
        return int(value)

    return None


def find_first_key_line_index(
    lines: list[str],
    column_config: "ColumnConfig",
) -> int | None:
    """
    Find the first element row on a continuation page, using the
    configured key column's shape (e.g. an element reference like
    "PER08") rather than a hardcoded pattern.

    Continuation pages may not contain 'Element Summary', but they
    usually contain rows like:

        PER08
        PO109
        PO110
    """

    for index, line in enumerate(lines):
        if column_config.is_key_line(line):
            return index

    return None


def parse_element_records_from_lines(
    lines: list[str],
    start_index: int,
    metadata: dict[str, str],
    page_number: int,
    column_config: "ColumnConfig",
    max_records: int | None = None,
) -> list[dict[str, Any]]:
    """
    Parse element records from a list of lines.

    Used for both:
    - normal segment page with Element Summary
    - continuation page without segment metadata
    """

    records = []
    i = start_index

    while i < len(lines):
        line = clean_line(lines[i])

        if is_stop_section(line):
            break

        if column_config.is_key_line(line):
            record, next_index = parse_element_record(
                lines=lines,
                start_index=i,
                metadata=metadata,
                page_number=page_number,
                column_config=column_config,
            )

            if record:
                records.append(record)

                if max_records is not None and len(records) >= max_records:
                    break

                # Safety: avoid infinite loop.
                if next_index <= i:
                    i += 1
                else:
                    i = next_index

                continue

        i += 1

    return records

    
def find_element_summary_start(lines: list[str]) -> int | None:
    """
    Find the Element Summary section.
    """

    for index, line in enumerate(lines):
        if line.startswith("Element Summary"):
            return index

    return None


def skip_element_summary_headers(
    lines: list[str],
    start_index: int,
    column_config: "ColumnConfig",
) -> int:
    """
    Skip the table header area after 'Element Summary'.

    The expected header row is whatever table columns the user
    specified (e.g. Ref, Id, Element Name, Req, Type, Min/Max, Usage,
    Rep, ...) — not a fixed 7-column assumption.
    """

    expected_headers = set(column_config.columns)

    i = start_index + 1
    seen_headers = 0

    while i < len(lines):
        line = lines[i]

        if line in expected_headers:
            seen_headers += 1
            i += 1
            continue

        if column_config.is_key_line(line):
            break

        # Stop header skipping after enough headers were seen.
        if seen_headers >= max(5, len(expected_headers) - 2):
            break

        i += 1

    return i


def parse_element_record(
    lines: list[str],
    start_index: int,
    metadata: dict[str, str],
    page_number: int,
    column_config: "ColumnConfig",
) -> tuple[dict[str, Any] | None, int]:
    """
    Parse one logical element record, walking the user-specified table
    columns in order instead of assuming a fixed Ref/Id/Element Name/
    Req/Type/Min-Max/Usage layout.

    The record starts at the key column's value (e.g. "BEG01") and ends
    before the next key-column value or a stop section (Comments,
    Semantics, ...). Every non-multiline column consumes exactly one
    line (mirroring how these single-token fields are laid out in the
    source PDF); the multi-line column consumes lines until the next
    column's shape is recognized. A trailing block of content that
    spills out after all the fixed-shape columns — a PDF-layout artifact
    from the metadata box sitting to the right of the element name — is
    collected separately and, if a multi-line column is configured,
    folded into that column's Description/Qualifiers/Details.
    """

    columns = column_config.columns
    i = start_index

    if i >= len(lines) or not column_config.is_key_line(lines[i]):
        return None, i

    values: dict[str, list[str]] = {col: [] for col in columns}
    values[column_config.key_column] = [lines[i]]
    i += 1

    key_idx = column_config.key_index

    # Secondary sanity check, mirroring the original parser's behavior:
    # if the column immediately after the key column has a recognizable
    # shape (id/req/type/min-max) and isn't the multi-line column,
    # require it to match before accepting this as a real record — this
    # is what rejects a stray key-shaped token found outside an actual
    # table row. Columns with no strong shape ("generic" role) aren't
    # strict enough to safely gate on, so they're skipped here.
    if key_idx + 1 < len(columns):
        second_col = columns[key_idx + 1]

        if second_col != column_config.multiline_column:
            if infer_column_role(second_col) != "generic":
                if i >= len(lines) or not column_config.detector_for(second_col)(lines[i]):
                    return None, i

    early_multiline_lines: list[str] = []

    for idx in range(key_idx + 1, len(columns)):
        col = columns[idx]
        next_detector = (
            column_config.detector_for(columns[idx + 1])
            if idx + 1 < len(columns)
            else None
        )

        if col == column_config.multiline_column:
            collected: list[str] = []

            while i < len(lines):
                line = lines[i]

                if column_config.is_key_line(line) or is_stop_section(line):
                    break

                if next_detector is not None and next_detector(line):
                    break

                collected.append(line)
                i += 1

            early_multiline_lines = collected
            continue

        if i >= len(lines):
            continue

        precedes_multiline = (
            column_config.multiline_column is not None
            and idx < column_config.columns.index(column_config.multiline_column)
        )

        if precedes_multiline:
            # Column occurs before the multi-line column (e.g. "Id" right
            # after the key column) — still gate on is_key_line/stop
            # section, since a genuinely short/missing record here is a
            # real possibility this early in the row.
            if not (
                column_config.is_key_line(lines[i]) or is_stop_section(lines[i])
            ):
                values[col] = [lines[i]]
                i += 1
        else:
            # Column occurs after the multi-line column (Req, Type,
            # Min/Max, Usage, Rep, ...), or there is no multi-line
            # column at all. These are short single-token values that
            # can coincidentally match the key-line shape (e.g. a
            # data-type code like "N0" looks like an element reference)
            # — so, matching the original parser, grab whatever is here
            # unconditionally rather than gating on it.
            values[col] = [lines[i]]
            i += 1

    # Trailing block: content that spills out after the fixed-shape
    # columns (Req/Type/Min-Max/Usage/...) due to the PDF's two-column
    # layout — the qualifier Code/Name table and vendor notes visually
    # belong under the multi-line column but appear later in the linear
    # text stream, after the metadata box on the right.
    detail_lines: list[str] = []

    while i < len(lines):
        if column_config.is_key_line(lines[i]) or is_stop_section(lines[i]):
            break

        detail_lines.append(lines[i])
        i += 1

    record: dict[str, Any] = {
        "source_page": page_number,
        "segment_id": metadata.get("segment_id", ""),
        "segment_name": metadata.get("segment_name", ""),
        "needs_review": False,
        "review_reason": "",
    }

    qualifiers = ""
    qualifier_expected_count = None

    for col in columns:
        if col == column_config.multiline_column:
            element_name, split_early_lines = split_element_name_from_embedded_details(
                early_multiline_lines
            )

            all_detail_lines = split_early_lines + detail_lines

            qualifiers = extract_qualifiers(all_detail_lines, column_config)
            details = extract_qualifier_details(all_detail_lines, column_config)
            description = extract_description(all_detail_lines)
            qualifier_expected_count = extract_expected_qualifier_count(all_detail_lines)

            record[col] = element_name
            record["Description"] = description
            record["Qualifiers"] = qualifiers
            record["Details"] = details
        else:
            record[col] = " ".join(values.get(col, [])).strip()

    record["Loop"] = metadata.get("loop", "")
    record["Level"] = metadata.get("level", "")
    record["qualifier_expected_count"] = qualifier_expected_count
    record["qualifier_extracted_count"] = count_extracted_qualifiers(qualifiers)

    refresh_record_review_status(record)

    return record, i


def _looks_like_structural_table_marker(line: str) -> bool:
    """
    Detect the fixed table-header vocabulary used by the guide-generator
    tool itself (e.g. "CodeList Summary (...)", "Code Name"). This same
    wording appears across every vendor's PDF because it's part of the
    ASC X12 guide template, not vendor-authored content — matching it
    literally is not the kind of vendor-specific hardcoding we avoid
    elsewhere.
    """

    line = clean_line(line)

    return line.startswith("CodeList Summary") or line.lower() in {
        "code name",
    } or (
        line.lower() == "code"
    )


def split_element_name_from_embedded_details(
    name_lines: list[str],
) -> tuple[str, list[str]]:
    """
    Some PDFs extract Description or vendor notes into the same block as
    the Element Name (e.g. two-column PDF text getting interleaved). This
    function keeps only the actual name and moves the rest to detail lines.

    Unlike a naive "join everything into one string, then re-split it"
    approach, this looks for the first ORIGINAL raw line that itself
    starts a label ("Description:", "Adobe:", "CodeList Summary", ...)
    and splits there. Deciding the boundary at the line level — rather
    than scanning a merged string — avoids a subtle ambiguity: an Element
    Name is itself often a short run of capitalized words (e.g. "Entity
    Identifier Code"), which is visually indistinguishable from a label's
    leading words once everything is joined into one blob. Checking each
    line's own start keeps that ambiguity from ever coming up.

    Example:
        ["Functional Identifier Code", "Description: Code identifying..."]
        becomes:
        Element Name = "Functional Identifier Code"
        Detail lines = ["Description: Code identifying..."]
    """

    cleaned_lines = [clean_line(line) for line in name_lines]
    cleaned_lines = [line for line in cleaned_lines if line]

    if not cleaned_lines:
        return "", []

    split_index = None

    for index, line in enumerate(cleaned_lines):
        if _looks_like_structural_table_marker(line) or is_label_line(line):
            split_index = index
            break

    if split_index is None:
        return " ".join(cleaned_lines).strip(), []

    element_name = " ".join(cleaned_lines[:split_index]).strip()
    detail_lines = cleaned_lines[split_index:]

    return element_name, detail_lines


def find_code_name_start(detail_lines: list[str]) -> int | None:
    """
    Find the start of the Code / Name mini-table.

    It may appear as:
    Code Name

    or:
    Code
    Name
    """

    for index, line in enumerate(detail_lines):
        normalized = clean_line(line).lower().replace(" ", "")

        if normalized == "codename":
            return index + 1

        if clean_line(line).lower() == "code":
            if index + 1 < len(detail_lines):
                if clean_line(detail_lines[index + 1]).lower() == "name":
                    return index + 2

    return None


def _looks_like_prose(text: str) -> bool:
    """
    Distinguish a qualifier "Name" cell (a short phrase, e.g. "Buying
    Party (Purchaser)", "Party to Receive Invoice for Goods or
    Services") from an ordinary sentence/continuation of a note
    (mixed-case prose, e.g. "requested ship date provided at this
    detail level will overide the date provided in the DTM segment...").

    This is shape-based, not a hardcoded vendor word list. Word count is
    the primary signal — genuine Name cells are short, run-on sentences
    are not. A small set of very common English function words is used
    only as a secondary check for borderline-length text, so a short
    name with a couple of connector words ("... to Receive ... for
    Goods or Services") isn't misclassified.
    """

    words = text.split()

    if len(words) > 10:
        return True

    if len(words) <= 3:
        return False

    function_words = {
        "a", "an", "the", "at", "in", "on", "of", "for", "to", "will",
        "is", "are", "was", "were", "this", "that", "and", "or", "but",
        "provided", "when", "if",
    }

    function_word_count = sum(1 for w in words if w.lower() in function_words)

    return (function_word_count / len(words)) >= 0.5


def looks_like_qualifier_row(line: str) -> bool:
    """
    Detect whether a line probably starts a Code/Name row.

    Examples:
        SD Saturday
        ST Standard Class
        00 Original
        CIP Carriage and Insurance Paid To
    """

    line = clean_line(line)

    if not line:
        return False

    match = re.match(r"^([A-Z0-9]{1,8})\s+(.+)$", line)

    if match and not _looks_like_prose(match.group(2)):
        return True

    if is_code_token(line):
        return True

    return False


def extract_code_name_pairs_from_line(line: str) -> list[tuple[str, str]]:
    """
    Extract one or more Code/Name pairs from one line.

    Handles:
        SD Saturday

    and also two pairs packed onto one line by a two-column PDF layout:
        SG Standard Ground ST Standard Class

    Matches are anchored at the start of the line (and, for a second or
    later pair, immediately after the previous pair ends) rather than
    scanned for anywhere in the line. An unanchored scan would also
    match a short all-caps token mentioned mid-sentence in ordinary
    prose — e.g. a note like "If FOB01 equals ..." or "... if BEG02 =
    DS ..." — and mistake it for a qualifier code. Anchoring rules that
    out: a genuine qualifier row always starts right at the beginning of
    its line (or right where the previous same-line pair left off), a
    mid-sentence reference never does.
    """

    line = clean_line(line)

    if not line:
        return []

    # Match one code (2+ chars keeps a lone capital letter at the start
    # of an ordinary sentence from being mistaken for a code) followed by
    # text, stopping at the next chained code or the end of line.
    pattern = re.compile(
        r"^\s*([A-Z0-9]{1,8})\s+(.+?)(?=\s+[A-Z0-9]{1,8}\s+[A-Z][a-z]|\s*$)"
    )

    pairs = []
    pos = 0

    while pos < len(line):
        match = pattern.match(line, pos)

        if not match:
            break

        code = match.group(1).strip()
        name = match.group(2).strip()

        if code.lower() in {"code", "name"}:
            break

        if not name:
            break

        # Avoid extracting obvious non-qualifier examples.
        if "*" in code or "*" in name:
            break

        # Guard against matching a run-on sentence (e.g. a continuation
        # of a vendor note) as if it were a Code/Name row.
        if _looks_like_prose(name):
            break

        pairs.append((code, name))
        pos = match.end()

    return pairs



def is_code_token(line: str) -> bool:
    """
    Detect qualifier code.

    Examples:
    00
    05
    PP
    PU
    TP
    OR
    BY
    ST
    """

    return bool(re.match(r"^[A-Z0-9]{1,8}$", clean_line(line)))


def find_code_name_header_index(detail_lines: list[str]) -> int | None:
    """
    Return the index of the 'Code Name' header line (or the 'Code' line
    when the header is split across two lines).

    Returns None when no qualifier table is present.
    """

    for index, line in enumerate(detail_lines):
        normalized = clean_line(line).lower().replace(" ", "")

        if normalized == "codename":
            return index

        if clean_line(line).lower() == "code":
            if index + 1 < len(detail_lines):
                if clean_line(detail_lines[index + 1]).lower() == "name":
                    return index

    return None


_EMBEDDED_LABEL_RE = re.compile(
    r"(?:[A-Z][A-Za-z0-9&/.'\-]*\s+){0,4}[A-Z][A-Za-z0-9&/.'\-]*\s*\d{0,2}\s*:+\s"
)


def _split_at_embedded_notes(line: str) -> list[str]:
    """
    Split a line wherever a "<Label>:" style annotation starts mid-line
    (not at position 0), using the same shape-based detection as
    is_label_line() — so this works for the standard "Description:" /
    "User Note 1:" labels as well as vendor-specific ones like "Adobe:"
    or "COMPTIA:", without hardcoding any specific wording.

    Example:
        "Description: Code identifying an organizational entity, a
         physical location, property or an individual Adobe: All
         parties are required to process the purchase order."
        → ["Description: Code identifying an organizational entity, a
            physical location, property or an individual",
           "Adobe: All parties are required to process the purchase
            order."]
    """

    split_positions: list[int] = []

    for m in _EMBEDDED_LABEL_RE.finditer(line):
        # Only mid-line matches count as a split point; a label at the
        # very start of the line is the current segment's own label.
        if m.start() > 0 and (m.end() - m.start()) <= 46:
            split_positions.append(m.start())

    if not split_positions:
        return [line]

    split_positions = sorted(set(split_positions))
    segments: list[str] = []
    last_pos = 0

    for pos in split_positions:
        before = line[last_pos:pos].strip()

        if before:
            segments.append(before)

        last_pos = pos

    remaining = line[last_pos:].strip()

    if remaining:
        segments.append(remaining)

    return segments if segments else [line]


def extract_description(detail_lines: list[str]) -> str:
    """
    Extract the description / notes text that appears below the element name
    and before the Code/Name qualifier table.

    Rules:
    - Stop at the 'Code Name' header (qualifier table start).
    - Skip 'CodeList Summary' lines.
    - Split embedded note labels to their own lines.
    - Deduplicate: if the same text appears twice (e.g. once embedded in the
      description string and once as a standalone line), keep only the first.
    """

    header_index = find_code_name_header_index(detail_lines)
    end = header_index if header_index is not None else len(detail_lines)

    desc_lines: list[str] = []
    seen: set[str] = set()

    for line in detail_lines[:end]:
        line = clean_line(line)

        if not line:
            continue

        if line.startswith("CodeList Summary") or line.startswith("Code List Summary"):
            continue

        # Split at note label boundaries embedded mid-line.
        for sub in _split_at_embedded_notes(line):
            sub = _deduplicate_text_halves(sub.strip())

            if sub and sub not in seen:
                desc_lines.append(sub)
                seen.add(sub)

    result = _join_wrapped_lines(desc_lines)

    if result.startswith("Description:"):
        result = result[len("Description:"):].lstrip()

    return result


_BULLET_LINE_RE = re.compile(r"^(?:\d{1,2}[.)]|[a-zA-Z][.)]|[-•*])\s+")

_SENTENCE_END_CHARS = (".", "!", "?", ":", ";")

# Common abbreviations whose trailing "." should NOT be treated as a
# sentence end (otherwise "e.g." or "U.S." would look like a paragraph
# break). Kept short and generic — this is about punctuation shape, not
# document content.
_ABBREVIATION_TAIL_RE = re.compile(
    r"(?:\b[A-Za-z]\.[A-Za-z](?:\.[A-Za-z])*|"
    r"\b(?:e\.g|i\.e|etc|vs|no|approx)\.)$",
    flags=re.IGNORECASE,
)


def _ends_sentence(line: str) -> bool:
    """
    Decide whether ``line`` looks like it completes a sentence/clause,
    as opposed to being cut off mid-sentence by PDF column wrapping.
    """

    line = line.rstrip()

    if not line:
        return False

    if not line.endswith(_SENTENCE_END_CHARS):
        return False

    # Guard against abbreviations like "U.S." or "e.g." that end in a
    # period but are not actually a sentence boundary.
    if line.endswith(".") and _ABBREVIATION_TAIL_RE.search(line):
        return False

    return True


def _looks_like_bullet_start(line: str) -> bool:
    return bool(_BULLET_LINE_RE.match(line.strip()))


_LABEL_ONLY_RE = re.compile(
    r"^(?:[A-Z][A-Za-z0-9&/.'\-]*\s+){0,4}[A-Z][A-Za-z0-9&/.'\-]*\s*\d{0,2}\s*:+$"
)


def _is_label_only_line(line: str) -> bool:
    """
    True when a line is just a "<Label>:" heading with no content after
    the colon — the content follows on the next line(s) instead, e.g.:
        Adobe:
        A requested ship date provided at this detail level...

    or:
        Dot Foods:
        If FOB01 equals "PC", Dot will bill the customer for freight.

    Such a label always expects its content to continue on the next
    line, even though the trailing colon would otherwise look like it
    ends a sentence/clause. Uses its own pattern (rather than
    is_label_line, which requires content after the colon and so can
    never match a bare label) — same shape-based rule, just without the
    trailing-content requirement.
    """

    return bool(_LABEL_ONLY_RE.match(clean_line(line)))


def should_start_new_line(previous_line: str, next_line: str) -> bool:
    """
    Decide whether ``next_line`` should begin a new logical line, or be
    merged into ``previous_line`` because the break was only an artifact
    of the PDF's column width.

    Signals used (in priority order):
      0. previous_line is a bare "<Label>:" with nothing after the colon
         -> merge (the label's content starts on this next line), unless
         next_line itself starts a new label or bullet.
      1. next_line is a new "<Label>:" style heading -> new line.
      2. next_line is a numbered/bulleted item -> new line.
      3. next_line starts with a lowercase letter -> merge (mid-sentence
         wrap; a real new sentence never starts lowercase).
      4. previous_line does not yet end a sentence/clause -> merge (still
         wrapping the same clause even though the next word is
         capitalized, e.g. a proper noun).
      5. previous_line ends a sentence/clause AND next_line starts like a
         new sentence -> new line (a genuine paragraph/sentence break).
    """

    previous_line = previous_line.strip()
    next_line = next_line.strip()

    if not next_line:
        return False

    if not previous_line:
        return True

    if _is_label_only_line(previous_line):
        if is_label_line(next_line) or _looks_like_bullet_start(next_line):
            return True

        return False

    if is_label_line(next_line):
        return True

    if _looks_like_bullet_start(next_line):
        return True

    if next_line[0].islower():
        return False

    if not _ends_sentence(previous_line):
        return False

    return True



def _join_wrapped_lines(lines: list[str]) -> str:
    """
    Join text lines, collapsing PDF soft-wrap artifacts while preserving
    genuine logical breaks (labels, bullets, sentence boundaries).

    Uses should_start_new_line() to decide, line by line, whether the next
    line begins a new logical line (kept on its own line, joined with
    '\\n') or is a wrapped continuation of the current one (merged with a
    single space).
    """

    if not lines:
        return ""

    out_lines: list[str] = [lines[0]]

    for line in lines[1:]:
        if not line:
            continue

        if should_start_new_line(out_lines[-1], line):
            out_lines.append(line)
        else:
            out_lines[-1] = (out_lines[-1] + " " + line).strip()

    return "\n".join(out_lines)


def _deduplicate_text_halves(text: str) -> str:
    """
    Collapse immediately-repeated word runs caused by PDF two-column text
    interleaving.

    A naive whole-string half-split ("does the first half of the text
    equal the second half?") misses a common variant of this artifact,
    where the label and its content are duplicated as *separate* runs
    rather than the whole phrase duplicating as one block:

        "User Note 1: User Note 1: <content> <content>"
        (label doubled, then content doubled — not "<label> <content>"
        repeated as a single unit)

    This scans left to right and, at each position, greedily collapses
    the largest run of words that's immediately followed by an identical
    repeat of itself, then continues from there. That handles the label
    and the content doubling independently, in one pass:

        "User Note 1: User Note 1: You will only receive the Delivery
         Appointment Scheduled Date if a prescheduled appointment has
         been made for you You will only receive the Delivery
         Appointment Scheduled Date if a prescheduled appointment has
         been made for you"
        → "User Note 1: You will only receive the Delivery Appointment
           Scheduled Date if a prescheduled appointment has been made
           for you"
    """

    words = text.split()
    n = len(words)
    result: list[str] = []
    i = 0

    while i < n:
        matched = False
        max_run = (n - i) // 2

        for run_len in range(max_run, 0, -1):
            if words[i:i + run_len] == words[i + run_len:i + 2 * run_len]:
                result.extend(words[i:i + run_len])
                i += 2 * run_len
                matched = True
                break

        if not matched:
            result.append(words[i])
            i += 1

    return " ".join(result)


def extract_qualifier_pairs_with_details(
    detail_lines: list[str],
    column_config: "ColumnConfig | None" = None,
) -> list[tuple[str, str, str]]:
    """
    Extract Code/Name qualifier pairs, keeping any trailing note lines as
    extra detail associated with that qualifier.

    Returns a list of (code, name, extra_notes) tuples where:
    - code          e.g. "Z7"
    - name          first line of the Name cell only, e.g. "Mark-for Party"
    - extra_notes   joined note lines after the first name line, e.g.
                    "User Note 1:Required when BEG02 = KN", or "" if none
    """

    is_key_line = column_config.is_key_line if column_config else is_ref_line

    start_index = find_code_name_start(detail_lines)

    if start_index is None:
        return []

    # pairs holds [code, name, extra_lines] while building, so extra detail
    # can be appended incrementally as trailing lines are discovered.
    pairs: list[list[Any]] = []
    i = start_index

    while i < len(detail_lines):
        line = clean_line(detail_lines[i])

        if not line:
            i += 1
            continue

        if is_key_line(line) or is_stop_section(line):
            break

        if line.lower() in {"code", "name", "code name"}:
            i += 1
            continue

        if line.startswith("CodeList Summary") or line.startswith("Code List Summary"):
            i += 1
            continue

        # Example blocks end qualifier extraction.
        if line.startswith("Example:") or line.startswith("Examples:"):
            break

        # Case 1: code and name on the same line (one or more pairs).
        same_line_pairs = extract_code_name_pairs_from_line(line)

        if same_line_pairs:
            for code, name in same_line_pairs:
                pairs.append([code, name, []])

            i += 1
            continue

        # Case 2: code and name on separate lines. The next line only
        # counts as a qualifier "Name" if it doesn't itself look like a
        # code token, a "<Label>:" annotation (Description:, Adobe:, ...),
        # or an ordinary sentence (prose continuation of a note).
        if is_code_token(line) and i + 1 < len(detail_lines):
            next_line = clean_line(detail_lines[i + 1])

            if (
                next_line
                and not is_code_token(next_line)
                and not is_label_line(next_line)
                and not _looks_like_prose(next_line)
                and not next_line.startswith("CodeList Summary")
                and not next_line.startswith("Code List Summary")
            ):
                pairs.append([line, next_line, []])
                i += 2
                continue

        # Anything else that reaches here is extra detail/notes trailing
        # the most recently found qualifier pair — this covers the
        # standard "Description:" label, vendor-specific labels like
        # "Adobe:" or "COMPTIA:", and plain continuation sentences,
        # WITHOUT hardcoding any specific label text. If there is no
        # qualifier pair yet, the line is a stray artifact and is skipped.
        if pairs and not looks_like_qualifier_row(line):
            pairs[-1][2].append(_deduplicate_text_halves(line))

        i += 1

    # Build final (code, name, extra_text) tuples, collapsing the
    # collected extra lines with the same wrap-vs-break heuristic used
    # for the Description column, and deduplicate by (code, name).
    unique: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for code, name, extra_lines in pairs:
        key = f"{code}={name}"

        if key in seen:
            continue

        seen.add(key)
        extra_text = _join_wrapped_lines(extra_lines).replace("\n", " ").strip()
        unique.append((code, name, extra_text))

    return unique


def extract_qualifiers(
    detail_lines: list[str],
    column_config: "ColumnConfig | None" = None,
) -> str:
    """
    Extract Code / Name pairs from the mini-table.

    Returns one 'Code = Name' entry per line, using only the first line of
    each Name cell so that embedded notes do not bleed into the qualifier text.
    """

    pairs = extract_qualifier_pairs_with_details(detail_lines, column_config)
    return "\n".join(f"{code} = {name}" for code, name, _ in pairs)


def extract_qualifier_details(
    detail_lines: list[str],
    column_config: "ColumnConfig | None" = None,
) -> str:
    """
    Like extract_qualifiers but appends any qualifier-level notes in [brackets].

    Example output:
        ST = Ship To
        Z7 = Mark-for Party [User Note 1:Required when BEG02 = KN]
    """

    pairs = extract_qualifier_pairs_with_details(detail_lines, column_config)
    parts = []

    for code, name, extra in pairs:
        if extra:
            parts.append(f"{code} = {name} [{extra}]")
        else:
            parts.append(f"{code} = {name}")

    return "\n".join(parts)


def extract_expected_qualifier_count(detail_lines: list[str]) -> int | None:
    """
    Extract expected qualifier count from CodeList Summary.

    Example:
        CodeList Summary (Total Codes: 66, Included: 15)
        → 15
    """

    for line in detail_lines:
        line = clean_line(line)

        match = re.search(r"Included:\s*([0-9]+)", line, flags=re.IGNORECASE)

        if match:
            return int(match.group(1))

    return None


def count_extracted_qualifiers(qualifiers: str) -> int:
    """
    Count extracted qualifier lines.

    Example:
        3D = Three Day Service
        CG = Ground

        → 2
    """

    if not qualifiers:
        return 0

    return len(
        [
            line
            for line in str(qualifiers).splitlines()
            if "=" in line and line.strip()
        ]
    )


def refresh_record_review_status(record: dict[str, Any]) -> None:
    """
    Update needs_review and review_reason based on qualifier completeness.
    """

    expected = record.get("qualifier_expected_count")
    actual = count_extracted_qualifiers(record.get("Qualifiers", ""))

    record["qualifier_extracted_count"] = actual

    review_reasons = []

    if expected is not None and expected != "":
        try:
            expected_int = int(expected)

            if actual != expected_int:
                review_reasons.append(
                    f"Qualifier count mismatch: expected {expected_int}, extracted {actual}."
                )

        except ValueError:
            pass

    record["needs_review"] = bool(review_reasons)
    record["review_reason"] = "\n".join(review_reasons)


def last_record_has_incomplete_qualifiers(
    records: list[dict[str, Any]],
    active_segment_metadata: dict[str, str] | None,
) -> bool:
    """
    Check whether the last extracted element still needs qualifier continuation.

    This is important when the segment reached its Elements count,
    but the last element's Code/Name mini-table continues on the next page.
    """

    if not records:
        return False

    if active_segment_metadata is None:
        return False

    last_record = records[-1]

    if last_record.get("segment_id") != active_segment_metadata.get("segment_id"):
        return False

    expected = last_record.get("qualifier_expected_count")
    actual = last_record.get("qualifier_extracted_count", 0)

    if expected in [None, ""]:
        return False

    try:
        return int(actual) < int(expected)
    except ValueError:
        return False
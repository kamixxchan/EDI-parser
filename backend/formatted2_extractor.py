import re
from typing import Any

import fitz  # PyMuPDF
import pandas as pd


FORMATTED2_OUTPUT_COLUMNS = [
    "source_page",
    "segment_id",
    "segment_name",
    "needs_review",
    "review_reason",
    "Ref",
    "Id",
    "Element Name",
    "Description",
    "Qualifiers",
    "Details",
    "Req",
    "Type",
    "Min/Max",
    "Loop",
    "Level",
]


STOP_SECTION_PREFIXES = (
    "Sample Segment",
    "Sample Segments",
    "Implementation Notes",
    "Transaction Set Notes",
    "Example:",
    "Examples:",
    "EXAMPLE:",
    "Appendix",
)


METADATA_LABELS = {
    "Segment",
    "Position",
    "Loop",
    "Level",
    "Usage",
    "Max Use",
    "Purpose",
    "Syntax",
    "Syntax Notes",
    "Semantic Notes",
    "Semantics",
    "Comments",
    "Comment",
    "Notes",
    "Note",
    "Example",
}

def infer_segment_id_from_ref(ref: str) -> str:
    """
    Infer segment ID from element reference.

    Examples:
        BEG01 -> BEG
        CUR01 -> CUR
        PER03 -> PER
        PO109 -> PO1
        TD512 -> TD5
    """

    ref = clean_line(ref)

    match = re.match(r"^([A-Z]{1,4}\d?)(\d{2})$", ref)

    if match:
        return match.group(1)

    return ""

def extract_formatted2_tables_from_pdf(
    pdf_path: str,
    page_start: int,
    page_end: int,
    expected_columns: list[str],
    multiline_column: str | None = None,
    include_audit_columns: bool = True,
    debug: bool = False,
) -> pd.DataFrame:
    """
    Layout-aware Formatted 2 extractor.

    Main idea:
    - User-provided table columns are used only as anchors for table start.
    - Actual extraction relies on visual row structure and EDI element patterns.
    - The parser reads word coordinates, so it can separate:
        Ref / Id / Name / Attributes
      even when PyMuPDF text extraction returns a strange text order.
    """

    if page_start < 1:
        raise ValueError("page_start must be 1 or greater.")

    if page_end < page_start:
        raise ValueError("page_end must be greater than or equal to page_start.")

    page_lines = read_pdf_visual_lines(
        pdf_path=pdf_path,
        page_start=page_start,
        page_end=page_end,
    )

    if debug:
        print("\n===== FORMATTED 2 DEBUG: VISUAL LINES =====")
        for item in page_lines[:250]:
            print(f"PAGE {item['page']}: {item['text']}")

    segment_blocks = split_lines_into_segment_blocks(page_lines)

    if debug:
        print("\n===== FORMATTED 2 DEBUG: SEGMENT BLOCK COUNT =====")
        print(f"Segment blocks found: {len(segment_blocks)}")

        for idx, block in enumerate(segment_blocks[:5]):
            print(f"\n--- BLOCK {idx + 1} ---")
            for item in block[:80]:
                print(f"PAGE {item['page']}: {item['text']}")

    records: list[dict[str, Any]] = []

    for block in segment_blocks:
        records.extend(
            parse_segment_block(
                block=block,
                table_anchor_columns=expected_columns,
            )
        )

    output_columns = FORMATTED2_OUTPUT_COLUMNS.copy()

    if not include_audit_columns:
        output_columns = [
            col
            for col in output_columns
            if col
            not in {
                "source_page",
                "segment_id",
                "segment_name",
                "needs_review",
                "review_reason",
            }
        ]

    if not records:
        output_columns = [c for c in output_columns if c != "Details"]
        return pd.DataFrame(columns=output_columns)

    df = pd.DataFrame(records)

    details_has_notes = (
        "Details" in df.columns
        and df["Details"].astype(str).str.contains(r"\[", regex=True).any()
    )

    if not details_has_notes:
        output_columns = [c for c in output_columns if c != "Details"]

    for col in output_columns:
        if col not in df.columns:
            df[col] = ""

    return df[output_columns]


# =====================================================
# PDF WORD / VISUAL LINE EXTRACTION
# =====================================================

def read_pdf_visual_lines(
    pdf_path: str,
    page_start: int,
    page_end: int,
) -> list[dict[str, Any]]:
    """
    Read selected PDF pages using word coordinates.

    Also detects whether each visual line overlaps a gray background rectangle.
    Gray background is useful for identifying notes/comments attached to qualifiers.

    Each returned line is also tagged with whether it sits in the page's
    top/bottom margin zone, which is used later to safely strip repeating
    page header/footer boilerplate without touching in-table content.
    """

    all_lines: list[dict[str, Any]] = []

    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)
        safe_start = max(1, page_start)
        safe_end = min(page_end, total_pages)

        for page_number in range(safe_start, safe_end + 1):
            page = doc[page_number - 1]
            page_height = float(page.rect.height) or 792.0

            gray_rectangles = extract_gray_rectangles(page)
            words = page.get_text("words") or []

            word_items = []

            for word in words:
                x0, y0, x1, y1, text, block_no, line_no, word_no = word

                clean_text = clean_line(text)

                if not clean_text:
                    continue

                word_items.append(
                    {
                        "page": page_number,
                        "text": clean_text,
                        "x0": float(x0),
                        "y0": float(y0),
                        "x1": float(x1),
                        "y1": float(y1),
                    }
                )

            page_lines = group_words_into_visual_lines(
                words=word_items,
                gray_rectangles=gray_rectangles,
            )

            margin_top = page_height * 0.15
            margin_bottom = page_height * 0.85

            for page_line in page_lines:
                page_line["in_margin_zone"] = (
                    page_line["y0"] < margin_top or page_line["y0"] > margin_bottom
                )

            all_lines.extend(page_lines)

    return filter_boilerplate_lines(all_lines)


def filter_boilerplate_lines(
    lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Remove repeating page header/footer boilerplate.

    Many EDI guides repeat a title/version/page-number line in the top or
    bottom margin of every page. These are detected generically (no
    hard-coded document strings):

    - the line must sit in the page's margin zone (top/bottom ~15%), and
    - its text, after blanking digit runs (so changing page numbers still
      match), must repeat across several distinct pages.

    Segment metadata labels (Segment:, Position:, Loop:, Level:, ...) and
    bare segment-id labels are never eligible, regardless of frequency or
    position, since those are required for correct parsing.
    """

    if not lines:
        return lines

    def normalize_for_frequency(text: str) -> str:
        return re.sub(r"\d+", "#", clean_line(text))

    def is_protected(text: str) -> bool:
        text = clean_line(text)

        if not text:
            return True

        if "Segment:" in text:
            return True

        if "Data Element Summary" in text:
            return True

        if is_metadata_label_line(text):
            return True

        if re.fullmatch(r"[A-Z]{1,4}[0-9]?", text):
            return True

        return False

    pages_by_key: dict[str, set[int]] = {}

    for item in lines:
        if not item.get("in_margin_zone"):
            continue

        if is_protected(item["text"]):
            continue

        key = normalize_for_frequency(item["text"])

        if not key:
            continue

        pages_by_key.setdefault(key, set()).add(item["page"])

    total_pages = len({item["page"] for item in lines})
    threshold = min(3, total_pages) if total_pages else 0

    boilerplate_keys = {
        key
        for key, pages in pages_by_key.items()
        if threshold and len(pages) >= threshold
    }

    if not boilerplate_keys:
        return lines

    return [
        item
        for item in lines
        if not (
            item.get("in_margin_zone")
            and not is_protected(item["text"])
            and normalize_for_frequency(item["text"]) in boilerplate_keys
        )
    ]

def is_gray_color(color: tuple | None, tolerance: float = 0.12) -> bool:
    """
    Detect gray-ish fill color from PDF drawing objects.
    """

    if color is None:
        return False

    if len(color) < 3:
        return False

    r, g, b = color[:3]

    channels_close = (
        abs(r - g) <= tolerance
        and abs(g - b) <= tolerance
        and abs(r - b) <= tolerance
    )

    not_white = not (r > 0.95 and g > 0.95 and b > 0.95)
    not_black = not (r < 0.10 and g < 0.10 and b < 0.10)

    return channels_close and not_white and not_black


def extract_gray_rectangles(page: fitz.Page) -> list[fitz.Rect]:
    """
    Extract gray filled rectangles from a PDF page.

    Many EDI guides use gray rectangles behind notes/examples.
    """

    gray_rectangles: list[fitz.Rect] = []

    for drawing in page.get_drawings():
        fill_color = drawing.get("fill")

        if not is_gray_color(fill_color):
            continue

        for item in drawing.get("items", []):
            if item[0] == "re":
                gray_rectangles.append(item[1])

    return gray_rectangles


def rect_overlaps_any_gray_rect(
    rect: fitz.Rect,
    gray_rectangles: list[fitz.Rect],
) -> bool:
    """
    Check whether a text line overlaps any gray rectangle.
    """

    return any(rect.intersects(gray_rect) for gray_rect in gray_rectangles)

def group_words_into_visual_lines(
    words: list[dict[str, Any]],
    gray_rectangles: list[fitz.Rect] | None = None,
    y_tolerance: float = 3.5,
) -> list[dict[str, Any]]:
    """
    Group words into visual rows by y-coordinate.

    Adds:
        has_gray_background = True/False
    """

    gray_rectangles = gray_rectangles or []

    if not words:
        return []

    words = sorted(words, key=lambda w: (w["y0"], w["x0"]))

    grouped: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_y: float | None = None

    for word in words:
        if current_y is None:
            current_y = word["y0"]
            current = [word]
            continue

        if abs(word["y0"] - current_y) <= y_tolerance:
            current.append(word)
        else:
            grouped.append(sorted(current, key=lambda w: w["x0"]))
            current = [word]
            current_y = word["y0"]

    if current:
        grouped.append(sorted(current, key=lambda w: w["x0"]))

    lines = []

    for group in grouped:
        text = " ".join(w["text"] for w in group)

        line_rect = fitz.Rect(
            min(w["x0"] for w in group),
            min(w["y0"] for w in group),
            max(w["x1"] for w in group),
            max(w["y1"] for w in group),
        )

        lines.append(
            {
                "page": group[0]["page"],
                "text": clean_line(text),
                "words": group,
                "x0": line_rect.x0,
                "x1": line_rect.x1,
                "y0": line_rect.y0,
                "y1": line_rect.y1,
                "has_gray_background": rect_overlaps_any_gray_rect(
                    rect=line_rect,
                    gray_rectangles=gray_rectangles,
                ),
            }
        )

    return lines

# =====================================================
# SEGMENT BLOCK PARSING
# =====================================================

def split_lines_into_segment_blocks(
    page_lines: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """
    Split document into segment blocks.

    Supports:
        Segment: SAC Service...
    and:
        Segment:
        SAC Service...

    Many EDI guides also print a large standalone segment-id label (e.g. a
    big bold "BEG") just above the "Segment:" line, on its own visual line.
    That bare label is treated as part of the boundary (i.e. it starts the
    new block) instead of being left behind as trailing text of the
    previous block.
    """

    boundary_indexes: set[int] = set()

    for index, item in enumerate(page_lines):
        if "Segment:" not in item["text"]:
            continue

        boundary_index = index

        for lookback in range(1, 4):
            candidate_index = index - lookback

            if candidate_index < 0:
                break

            candidate_text = clean_line(page_lines[candidate_index]["text"])

            if not candidate_text:
                continue

            if re.fullmatch(r"[A-Z]{1,4}[0-9]?", candidate_text):
                boundary_index = candidate_index
                continue

            break

        boundary_indexes.add(boundary_index)

    if not boundary_indexes:
        return []

    sorted_boundaries = sorted(boundary_indexes)

    blocks: list[list[dict[str, Any]]] = []

    for position, start_index in enumerate(sorted_boundaries):
        end_index = (
            sorted_boundaries[position + 1]
            if position + 1 < len(sorted_boundaries)
            else len(page_lines)
        )

        blocks.append(page_lines[start_index:end_index])

    return blocks


def parse_segment_block(
    block: list[dict[str, Any]],
    table_anchor_columns: list[str],
) -> list[dict[str, Any]]:
    """
    Parse one segment block into element rows.
    """

    metadata = parse_segment_metadata(block)

    table_start = find_table_start_index(
        lines=block,
        table_anchor_columns=table_anchor_columns,
    )

    if table_start is None:
        return []

    records: list[dict[str, Any]] = []
    i = table_start
    pending_row: tuple[dict[str, Any], int] | None = None

    while i < len(block):
        line = block[i]

        if is_stop_section(line["text"]):
            break

        if pending_row is not None:
            row_start, lines_consumed = pending_row
            pending_row = None
        else:
            result = parse_main_element_row_with_continuation(block, i)

            if result is None:
                i += 1
                continue

            row_start, lines_consumed = result

        detail_lines: list[dict[str, Any]] = []
        j = i + lines_consumed
        next_row_peek: tuple[dict[str, Any], int] | None = None

        while j < len(block):
            next_line = block[j]

            if is_stop_section(next_line["text"]):
                break

            next_row_peek = parse_main_element_row_with_continuation(block, j)

            if next_row_peek is not None:
                break

            detail_lines.append(next_line)
            j += 1

        # Some guides vertically center a wrapped Element Name, so its
        # first line can render ABOVE the row's Ref/Attributes line,
        # ending up as a trailing "detail line" of the PREVIOUS row
        # instead of the start of the name that follows. Detect this by
        # column alignment: if the last unconsumed line sits in exactly
        # the same x-position as the next row's name, with nothing else
        # in between, it's the wrapped name's first line, not a comment.
        if next_row_peek is not None and len(detail_lines) == 1:
            candidate = detail_lines[-1]
            candidate_text = clean_line(candidate["text"])
            next_row_start = next_row_peek[0]

            if (
                candidate_text
                and not is_code_name_header(candidate_text)
                and not parse_qualifier_pair_from_visual_line(candidate, row_start)
                and len(next_row_start["element_name"].split()) == 1
                and abs(candidate["x0"] - next_row_start["name_x0"]) <= 6
            ):
                detail_lines.pop()
                next_row_start["element_name"] = clean_line(
                    f"{candidate_text} {next_row_start['element_name']}"
                )
                pending_row = (next_row_start, next_row_peek[1])

        record = build_record(
            row_start=row_start,
            detail_lines=detail_lines,
            source_page=line["page"],
            metadata=metadata,
        )

        records.append(record)
        i = j

    return records


# =====================================================
# METADATA
# =====================================================

def parse_segment_metadata(lines: list[dict[str, Any]]) -> dict[str, str]:
    """
    Extract segment_id, segment_name, loop, and level.

    Handles:
        Segment: BEG Beginning Segment for Purchase Order

    and:
        Segment:
        BEG Beginning Segment for Purchase Order

    and:
        Segment:
        BEG
        Beginning Segment for Purchase Order
    """

    text_lines = [item["text"] for item in lines]

    metadata = {
        "segment_id": "",
        "segment_name": "",
        "loop": "",
        "level": "",
    }

    segment_index = next(
        (idx for idx, line in enumerate(text_lines) if "Segment:" in line),
        None,
    )

    if segment_index is not None:
        segment_line = text_lines[segment_index]
        segment_text = segment_line.split("Segment:", 1)[1].strip()

        following_lines = []

        for idx in range(segment_index + 1, min(segment_index + 5, len(text_lines))):
            candidate = clean_line(text_lines[idx])

            if not candidate:
                continue

            if is_metadata_label_line(candidate):
                break

            following_lines.append(candidate)

        # Case 0: Segment: BEG   (bare id only) / Beginning Segment...
        # The id is given inline, but the name sits on the next line.
        if segment_text and re.match(r"^[A-Z0-9]{2,4}$", segment_text) and following_lines:
            metadata["segment_id"] = segment_text
            metadata["segment_name"] = following_lines[0]
            full_segment_text = ""

        # Case 1: Segment: BEG Beginning Segment...
        elif segment_text:
            full_segment_text = segment_text

        # Case 2: Segment: / BEG Beginning Segment...
        elif following_lines:
            # Case 3: Segment: / BEG / Beginning Segment...
            if (
                len(following_lines) >= 2
                and re.match(r"^[A-Z0-9]{2,4}$", following_lines[0])
            ):
                full_segment_text = following_lines[0] + " " + following_lines[1]
            else:
                full_segment_text = following_lines[0]
        else:
            full_segment_text = ""

        if not metadata["segment_id"] and not metadata["segment_name"]:
            match = re.match(r"^([A-Z0-9]{2,4})\s+(.+)$", full_segment_text)

            if match:
                metadata["segment_id"] = match.group(1).strip()
                metadata["segment_name"] = match.group(2).strip()
            else:
                metadata["segment_name"] = full_segment_text

    metadata["loop"] = clean_loop_value(read_metadata_value(text_lines, "Loop"))
    metadata["level"] = read_metadata_value(text_lines, "Level")

    return metadata


def read_metadata_value(lines: list[str], label: str) -> str:
    """
    Read metadata value from:
        Loop: SAC Optional

    or:
        Loop:
        SAC Optional

    If the value is empty and the next line is another metadata label,
    return empty string.
    """

    for index, line in enumerate(lines[:100]):
        line = clean_line(line)

        if line.startswith(f"{label}:"):
            same_line_value = line.split(":", 1)[1].strip()

            if same_line_value:
                return same_line_value

            if index + 1 < len(lines):
                next_line = clean_line(lines[index + 1])

                # Important fix:
                # If Loop: is empty and next line is Level: Heading,
                # do not treat Level as Loop value.
                if is_metadata_label_line(next_line):
                    return ""

                return next_line

    return ""


def clean_loop_value(value: str) -> str:
    """
    Clean Loop value.

    Examples:
        "" -> "NA"
        "SAC Optional" -> "SAC"
        "PO1 Mandatory" -> "PO1"
        "N/A" -> "NA"

    Prevents wrong cases like:
        "Level: Heading" -> "NA"
    """

    value = clean_line(value)

    if not value:
        return "NA"

    if value.startswith("Level:"):
        return "NA"

    if value.upper() in {"N/A", "NA"}:
        return "NA"

    parts = value.split()

    if not parts:
        return "NA"

    first = parts[0]

    if re.match(r"^[A-Z0-9]{1,6}$", first):
        return first

    return value


def is_metadata_label_line(line: str) -> bool:
    """
    Detect metadata label lines.

    Handles:
        Level:
        Level: Heading
        Usage:
        Usage: Optional
    """

    line = clean_line(line)

    for label in METADATA_LABELS:
        if line == f"{label}:":
            return True

        if line.startswith(f"{label}:"):
            return True

    return False

# =====================================================
# TABLE START
# =====================================================

def find_table_start_index(
    lines: list[dict[str, Any]],
    table_anchor_columns: list[str],
) -> int | None:
    """
    Find where the element table starts.

    The user-entered column names are only anchors.
    They are NOT treated as the final output schema.
    """

    search_start = 0

    for index, line in enumerate(lines):
        if line["text"].startswith("Data Element Summary"):
            search_start = index + 1
            break

    # Move search_start after the visible header area.
    anchor_terms = build_anchor_terms(table_anchor_columns)

    last_header_index = None

    for index in range(search_start, len(lines)):
        text = lines[index]["text"]

        if parse_main_element_row_with_continuation(lines, index) is not None:
            break

        if any(term == normalize_col(text) for term in anchor_terms):
            last_header_index = index

        # Common split-header tokens.
        if normalize_col(text) in {
            "ref",
            "des",
            "data",
            "element",
            "name",
            "attributes",
            "base",
            "user",
            "req",
            "type",
            "length",
            "min_max",
        }:
            last_header_index = index

    if last_header_index is not None:
        search_start = last_header_index + 1

    for index in range(search_start, len(lines)):
        if parse_main_element_row_with_continuation(lines, index) is not None:
            return index

    return None


def build_anchor_terms(columns: list[str]) -> set[str]:
    """
    Turn user-entered headers into loose anchor tokens.

    Example:
        'Reference Designator' -> {'reference_designator', 'reference', 'designator'}
    """

    terms = set()

    for col in columns:
        norm = normalize_col(col)

        if norm:
            terms.add(norm)

        for part in re.split(r"[^A-Za-z0-9]+", col):
            part_norm = normalize_col(part)

            if part_norm:
                terms.add(part_norm)

    return terms


# =====================================================
# MAIN ELEMENT ROW PARSING
# =====================================================

def parse_main_element_row(line: dict[str, Any]) -> dict[str, Any] | None:
    """
    Parse one visual table row.

    Example:
        SAC02 1300 Service, Promotion, Allowance, or Charge Code O ID 4/4

    Output:
        Ref = SAC02
        Element Number = 1300
        Element Name = Service, Promotion, Allowance, or Charge Code
        Req = O
        Type = ID
        Min/Max = 4/4
    """

    words = line.get("words", [])

    if not words:
        return None

    tokens = [w["text"] for w in words]

    ref_info = find_ref_at_row_start(tokens)

    if ref_info is None:
        return None

    ref, ref_start, ref_end = ref_info

    if ref_end >= len(tokens):
        return None

    element_number = tokens[ref_end]

    if not is_element_number(element_number):
        return None

    element_number_index = ref_end
    content_start_index = element_number_index + 1

    attr_info = find_attribute_sequence(tokens, content_start_index)

    if attr_info:
        attr_start, attr_end, attr = attr_info
        name_tokens = tokens[content_start_index:attr_start]
        user_attributes = " ".join(tokens[attr_end:]).strip()

        name_x0 = words[content_start_index]["x0"] if content_start_index < len(words) else line["x0"]
        attr_x0 = words[attr_start]["x0"] if attr_start < len(words) else line["x1"]
    else:
        attr_start = len(tokens)
        name_tokens = tokens[content_start_index:]
        user_attributes = ""

        attr = {
            "req": "",
            "data_type": "",
            "min_max": "",
            "attributes": "",
        }

        name_x0 = words[content_start_index]["x0"] if content_start_index < len(words) else line["x0"]
        attr_x0 = line["x1"]

    element_name = clean_line(" ".join(name_tokens))

    if not element_name:
        return None

    return {
        "ref": normalize_element_ref(ref),
        "element_number": clean_line(element_number),
        "element_name": element_name,
        "req": attr["req"],
        "data_type": attr["data_type"],
        "min_max": attr["min_max"],
        "attributes": attr["attributes"],
        "user_attributes": user_attributes,
        "comments_x0": None,
        "raw_row_text": line["text"],
        "line_x0": line["x0"],
        "line_x1": line["x1"],
        "name_x0": name_x0,
        "attr_x0": attr_x0,
    }


MAX_NAME_CONTINUATION_LINES = 3


def parse_main_element_row_with_continuation(
    block: list[dict[str, Any]],
    index: int,
) -> tuple[dict[str, Any], int] | None:
    """
    Parse a main element row, allowing the Element Name to wrap onto up to
    MAX_NAME_CONTINUATION_LINES following visual lines before the
    Req/Type/Min-Max attribute sequence is found.

    This handles layouts such as:
        Required ST02 329
        Transaction set Control
        Num M AN 4/9

    where the row-start line alone has nothing usable after the element
    number. Returns (row_start, lines_consumed) or None.
    """

    line = block[index]
    words = line.get("words", [])

    if not words:
        return None

    tokens = [w["text"] for w in words]

    ref_info = find_ref_at_row_start(tokens)

    if ref_info is None:
        return None

    ref, ref_start, ref_end = ref_info

    if ref_end >= len(tokens):
        return None

    element_number = tokens[ref_end]

    if not is_element_number(element_number):
        return None

    content_start_index = ref_end + 1

    combined_words = list(words[content_start_index:])
    combined_tokens = [w["text"] for w in combined_words]
    consumed_count = 1

    attr_info = find_attribute_sequence(combined_tokens, 0)

    extra_used = 0
    next_index = index + 1

    fallback_name_x0 = (
        words[content_start_index]["x0"]
        if content_start_index < len(words)
        else line["x1"]
    )

    while attr_info is None and extra_used < MAX_NAME_CONTINUATION_LINES and next_index < len(block):
        next_line = block[next_index]
        next_text = clean_line(next_line.get("text", ""))

        if not next_text:
            next_index += 1
            continue

        if is_stop_section(next_text):
            break

        if is_metadata_label_line(next_text):
            break

        if "Segment:" in next_text:
            break

        if parse_main_element_row(next_line) is not None:
            break

        if parse_qualifier_pair_from_visual_line(
            line=next_line,
            row_start={"name_x0": fallback_name_x0},
        ):
            break

        next_words = next_line.get("words", [])

        combined_words.extend(next_words)
        combined_tokens.extend(w["text"] for w in next_words)

        consumed_count += 1
        extra_used += 1
        next_index += 1

        attr_info = find_attribute_sequence(combined_tokens, 0)

    if attr_info:
        attr_start, attr_end, attr = attr_info
        name_tokens = combined_tokens[:attr_start]
        leftover_words = combined_words[attr_end:]
        user_attributes = clean_line(" ".join(w["text"] for w in leftover_words))
        name_x0 = combined_words[0]["x0"] if combined_words else fallback_name_x0
        attr_x0 = (
            combined_words[attr_start]["x0"]
            if attr_start < len(combined_words)
            else line["x1"]
        )
        comments_x0 = leftover_words[0]["x0"] if leftover_words else None
        attr_values = attr
    else:
        name_tokens = combined_tokens
        user_attributes = ""
        comments_x0 = None
        attr_values = {"req": "", "data_type": "", "min_max": "", "attributes": ""}
        name_x0 = combined_words[0]["x0"] if combined_words else fallback_name_x0
        attr_x0 = line["x1"]

    element_name = clean_line(" ".join(name_tokens))

    if not element_name:
        return None

    row_start = {
        "ref": normalize_element_ref(ref),
        "element_number": clean_line(element_number),
        "element_name": element_name,
        "req": attr_values["req"],
        "data_type": attr_values["data_type"],
        "min_max": attr_values["min_max"],
        "attributes": attr_values["attributes"],
        "user_attributes": user_attributes,
        "comments_x0": comments_x0,
        "raw_row_text": line["text"],
        "line_x0": line["x0"],
        "line_x1": line["x1"],
        "name_x0": name_x0,
        "attr_x0": attr_x0,
    }

    return row_start, consumed_count


def find_ref_at_row_start(tokens: list[str]) -> tuple[str, int, int] | None:
    """
    Find element reference at the beginning of a row.

    Supports:
        SAC01
        SAC 01
        PO109
        PO1 09
    """

    if not tokens:
        return None

    start = 0

    # Sometimes there is a leading required/optional marker.
    if is_leading_status(tokens[0]) and len(tokens) > 1:
        start = 1

    if start >= len(tokens):
        return None

    # Compact ref: SAC01
    if is_element_ref(tokens[start]):
        return tokens[start], start, start + 1

    # Split ref: SAC 01
    if start + 1 < len(tokens):
        possible_ref = f"{tokens[start]} {tokens[start + 1]}"

        if is_element_ref(possible_ref):
            return possible_ref, start, start + 2

    return None


def find_attribute_sequence(
    tokens: list[str],
    start_index: int,
) -> tuple[int, int, dict[str, str]] | None:
    """
    Find attribute sequence inside a visual row.

    Supports:
        M ID 1/1
        O ID 4/4
        C ID 1 / 3
        M ID 2/3 M

    The optional trailing token after Min/Max is treated as User Attributes.
    """

    for index in range(start_index, len(tokens)):
        if not is_req_code(tokens[index]):
            continue

        if index + 1 >= len(tokens):
            continue

        if not is_data_type(tokens[index + 1]):
            continue

        minmax_result = parse_minmax_from_tokens(tokens, index + 2)

        if minmax_result is None:
            continue

        minmax, minmax_end = minmax_result

        req = clean_line(tokens[index])
        data_type = clean_line(tokens[index + 1])

        attr = {
            "req": req,
            "data_type": data_type,
            "min_max": minmax,
            "attributes": f"{req} {data_type} {minmax}",
        }

        return index, minmax_end, attr

    return None


def parse_minmax_from_tokens(
    tokens: list[str],
    index: int,
) -> tuple[str, int] | None:
    """
    Parse:
        1/1
    or:
        1 / 1
    """

    if index >= len(tokens):
        return None

    value = clean_line(tokens[index])

    if re.match(r"^\d+\s*/\s*\d+$", value):
        return normalize_min_max(value), index + 1

    if index + 2 < len(tokens):
        if tokens[index].isdigit() and tokens[index + 1] == "/" and tokens[index + 2].isdigit():
            return f"{tokens[index]}/{tokens[index + 2]}", index + 3

    return None


# =====================================================
# DETAIL / QUALIFIER EXTRACTION
# =====================================================

def split_lines_by_comments_column(
    detail_lines: list[dict[str, Any]],
    comments_x0: float | None,
    tolerance: float = 40.0,
    min_gap: float = 40.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Split detail lines into (near, far) by horizontal position.

    Some guides print a genuinely separate "Comments" column far to the
    right of the Name column (e.g. Gentex). When both columns wrap onto
    several lines at the same height, naively joining all words on a
    visual line by x-position interleaves two unrelated paragraphs into
    nonsense. Splitting at that point keeps the two paragraphs intact and
    in order, instead of word-interleaved.

    A line is only split if it contains a genuine large horizontal gap
    (>= min_gap, well above normal word spacing) whose right-hand side
    lands at the comments-column anchor. An ordinary wide sentence that
    merely extends past the anchor's x-position -- with normal word
    spacing throughout -- is never split.
    """

    if comments_x0 is None:
        return detail_lines, []

    threshold = comments_x0 - tolerance
    near_lines: list[dict[str, Any]] = []
    far_lines: list[dict[str, Any]] = []

    for line in detail_lines:
        words = sorted(line.get("words", []), key=lambda w: w["x0"])

        split_at = None

        for word_index in range(1, len(words)):
            previous_word = words[word_index - 1]
            current_word = words[word_index]

            gap = current_word["x0"] - previous_word["x1"]

            if gap >= min_gap and current_word["x0"] >= threshold:
                split_at = word_index
                break

        # No internal gap (a single cluster): classify the whole line by
        # its own starting position instead.
        if split_at is None:
            if words and words[0]["x0"] >= threshold:
                split_at = 0
            else:
                near_lines.append(line)
                continue

        near_words = words[:split_at]
        far_words = words[split_at:]

        if near_words:
            near_line = dict(line)
            near_line["words"] = near_words
            near_line["text"] = clean_line(" ".join(w["text"] for w in near_words))
            near_line["x0"] = min(w["x0"] for w in near_words)
            near_lines.append(near_line)

        if far_words:
            far_line = dict(line)
            far_line["words"] = far_words
            far_line["text"] = clean_line(" ".join(w["text"] for w in far_words))
            far_line["x0"] = min(w["x0"] for w in far_words)
            far_lines.append(far_line)

    return near_lines, far_lines


def is_attribute_status_only(text: str) -> bool:
    """
    True for leftover fragments that are just a secondary attribute/usage
    marker (e.g. a duplicated "Must Use" / "Used" / "M" column value)
    rather than genuine prose. These are dropped instead of being mixed
    into Description.
    """

    normalized = clean_line(text).rstrip(".").lower()

    return normalized in {
        "must use",
        "must",
        "use",
        "used",
        "not used",
        "rec",
        "rec.",
        "not rec",
        "not rec.",
        "m",
        "o",
        "c",
        "x",
        "n",
        "",
    }


def build_record(
    row_start: dict[str, Any],
    detail_lines: list[dict[str, Any]],
    source_page: int,
    metadata: dict[str, str],
) -> dict[str, Any]:
    """
    Build final output row.
    """

    near_lines, far_lines = split_lines_by_comments_column(
        detail_lines=detail_lines,
        comments_x0=row_start.get("comments_x0"),
    )

    qualifiers, details, qualifier_line_indexes = extract_qualifiers_and_consumed_indexes(
        detail_lines=near_lines,
        row_start=row_start,
    )

    # notes = extract_notes(near_lines)
    description = extract_description(
        detail_lines=near_lines,
        consumed_indexes=qualifier_line_indexes,
    )

    comments_fragments = [row_start.get("user_attributes", "")]
    comments_fragments.extend(clean_line(line["text"]) for line in far_lines)

    comments_text = "\n".join(
        remove_duplicates(
            fragment
            for fragment in comments_fragments
            if fragment and not is_attribute_status_only(fragment)
        )
    )

    if comments_text:
        description = f"{description}\n{comments_text}".strip("\n") if description else comments_text

    # details = extract_details(
    #     detail_lines=near_lines,
    #     consumed_indexes=qualifier_line_indexes,
    # )

    segment_id = metadata.get("segment_id", "")

    if not segment_id:
        segment_id = infer_segment_id_from_ref(row_start.get("ref", ""))

    record = {
        "source_page": source_page,
        "segment_id": segment_id,
        "segment_name": metadata.get("segment_name", ""),
        "needs_review": False,
        "review_reason": "",
        "Ref": row_start.get("ref", ""),
        "Id": row_start.get("element_number", ""),
        "Element Name": row_start.get("element_name", ""),
        "Description": description,
        "Qualifiers": qualifiers,
        "Details": details,
        "Req": row_start.get("req", ""),
        "Type": row_start.get("data_type", ""),
        "Min/Max": row_start.get("min_max", ""),
        "Loop": metadata.get("loop", ""),
        "Level": metadata.get("level", ""),
    }

    review_reasons = []

    if not record["Ref"]:
        review_reasons.append("Missing element reference.")

    if not record["Element Name"]:
        review_reasons.append("Missing element name.")

    if not record["Level"]:
        review_reasons.append("Missing segment level.")

    if not record["Req"] or not record["Type"] or not record["Min/Max"]:
        review_reasons.append("Missing one or more attribute values.")

    record["needs_review"] = bool(review_reasons)
    record["review_reason"] = "\n".join(review_reasons)

    return record





def extract_notes(detail_lines: list[dict[str, Any]]) -> str:
    """
    Extract generic notes/comments.
    """

    notes = []
    current = []

    for line in detail_lines:
        text = clean_line(line["text"])

        if is_generic_note_start(text):
            if current:
                notes.append(" ".join(current).strip())

            current = [text]
            continue

        if current:
            if is_qualifier_like_line(text) or is_stop_section(text):
                notes.append(" ".join(current).strip())
                current = []
            else:
                current.append(text)

    if current:
        notes.append(" ".join(current).strip())

    return "\n".join(remove_duplicates(notes))


def extract_qualifiers_and_consumed_indexes(
    detail_lines: list[dict[str, Any]],
    row_start: dict[str, Any],
) -> tuple[str, str, set[int]]:
    """
    Extract qualifier mini-table rows.

    Important:
    This does NOT treat every all-caps/number token as a qualifier.
    It only treats a line as a qualifier if:
    - the first token is a valid code, and
    - the code/name are visually separated like a table row.

    Continuation lines in the right-side name column are attached in
    [brackets], but only in the returned "details" string — the
    returned "qualifiers" string stays plain "Code = Name" with no
    brackets.

    Returns (qualifiers, details, consumed_indexes) where:
    - qualifiers  "Code = Name" only, one per line
    - details     same, but with "[note]" appended when a qualifier has
                  trailing note text
    """

    consumed_indexes: set[int] = set()
    entries: list[dict[str, Any]] = []

    active_entry: dict[str, Any] | None = None

    for index, line in enumerate(detail_lines):
        text = clean_line(line["text"])

        if not text:
            continue

        if is_stop_section(text):
            break

        if is_code_name_header(text):
            consumed_indexes.add(index)
            continue

        pair = parse_qualifier_pair_from_visual_line(
            line=line,
            row_start=row_start,
        )

        if pair:
            if active_entry:
                entries.append(active_entry)

            code, name, name_x0, code_x0 = pair

            active_entry = {
                "code": code,
                "name": name,
                "name_x0": name_x0,
                "code_x0": code_x0,
                "notes": [],
            }

            consumed_indexes.add(index)
            continue

        if active_entry and is_qualifier_continuation_line(
            line=line,
            active_entry=active_entry,
            row_start=row_start,
        ):
            continuation_text = text

            aligns_with_code_column = (
                abs(line["x0"] - active_entry.get("code_x0", -1000)) <= 8
            )

            if aligns_with_code_column or should_append_continuation_to_qualifier_name(
                current_name=active_entry["name"],
                continuation_text=continuation_text,
            ):
                active_entry["name"] = clean_line(
                    active_entry["name"] + " " + continuation_text
                )
            else:
                active_entry["notes"].append(continuation_text)

            consumed_indexes.add(index)
            continue

    if active_entry:
        entries.append(active_entry)

    if not entries:
        floating_code_fallback = build_floating_code_qualifier(
            detail_lines=detail_lines,
            consumed_indexes=consumed_indexes,
        )

        if floating_code_fallback:
            entry, extra_consumed = floating_code_fallback
            entries.append(entry)
            consumed_indexes |= extra_consumed

    qualifiers_only = []
    details_with_notes = []

    for entry in entries:
        plain_value = f"{entry['code']} = {entry['name']}"
        qualifiers_only.append(plain_value)

        if entry["notes"]:
            bracket_text = " ".join(entry["notes"]).strip()
            details_with_notes.append(f"{plain_value} [{bracket_text}]")
        else:
            details_with_notes.append(plain_value)

    return (
        "\n".join(remove_duplicates(qualifiers_only)),
        "\n".join(remove_duplicates(details_with_notes)),
        consumed_indexes,
    )


def build_floating_code_qualifier(
    detail_lines: list[dict[str, Any]],
    consumed_indexes: set[int],
) -> tuple[dict[str, Any], set[int]] | None:
    """
    Handle the rare layout where a single qualifier code sits alone on its
    own visual line, vertically centered next to a description that wraps
    across several lines (e.g. Michaels REF01's 'PD'), rather than the
    usual same-line "CODE Name" pairing.

    Only fires when there is exactly one such floating code and at least
    one other line of real text to use as its name -- otherwise normal
    rows with a short stray line would be misread as qualifiers.
    """

    floating_indexes = [
        index
        for index, line in enumerate(detail_lines)
        if index not in consumed_indexes
        and len(line.get("words", [])) == 1
        and is_qualifier_code(clean_line(line["text"]))
    ]

    if len(floating_indexes) != 1:
        return None

    code_index = floating_indexes[0]

    name_fragments = []
    extra_consumed = {code_index}

    for index, line in enumerate(detail_lines):
        if index == code_index or index in consumed_indexes:
            continue

        text = clean_line(line["text"])

        if not text or is_stop_section(text) or is_code_name_header(text):
            continue

        name_fragments.append(text)
        extra_consumed.add(index)

    if not name_fragments:
        return None

    code = clean_line(detail_lines[code_index]["text"])
    name = clean_line(" ".join(name_fragments))

    entry = {"code": code, "name": name, "name_x0": 0.0, "code_x0": 0.0, "notes": []}

    return entry, extra_consumed


def parse_qualifier_pair_from_visual_line(
    line: dict[str, Any],
    row_start: dict[str, Any],
) -> tuple[str, str, float] | None:
    """
    Parse visual qualifier row.

    Good:
        BE        Blanket Order/Estimated Quantities
        C310      Discount

    Bad:
        3M requires the Currency Code...
        A free-form description...
        FOB07 field contains...
    """

    words = line.get("words", [])

    if len(words) < 2:
        return None

    first_word = words[0]
    second_word = words[1]

    code = clean_line(first_word["text"])

    if not is_qualifier_code(code):
        return None

    # Qualifier mini-table should be inside/near the Name column,
    # not at the far left of the page.
    name_column_x0 = float(row_start.get("name_x0", 0))

    if first_word["x0"] < name_column_x0 - 25:
        return None

    # Strong condition:
    # code and name should be visually separated like columns.
    visual_gap = second_word["x0"] - first_word["x1"]

    if visual_gap < 10:
        return None

    name = " ".join(w["text"] for w in words[1:]).strip()

    if not name:
        return None

    # Avoid descriptions accidentally treated as qualifier rows.
    if is_description_sentence(name):
        return None

    return code, clean_line(name), second_word["x0"], first_word["x0"]


def is_qualifier_continuation_line(
    line: dict[str, Any],
    active_entry: dict[str, Any],
    row_start: dict[str, Any],
) -> bool:
    """
    Detect continuation text belonging to the previous qualifier.

    Example:
        BE = Blanket Order...
        Commitment)
        [gray note...]

    The continuation usually appears in the right-side Name column.
    """

    text = clean_line(line["text"])

    if not text:
        return False

    if is_stop_section(text):
        return False

    if is_code_name_header(text):
        return False

    if parse_qualifier_pair_from_visual_line(line, row_start):
        return False

    # If gray background and located in/right of the qualifier name column,
    # treat it as qualifier continuation note.
    if line.get("has_gray_background"):
        return line["x0"] >= active_entry["name_x0"] - 20

    # Continuation in the same right-side cell, OR a plain wrapped line
    # that falls back to the qualifier's own code column.
    if line["x0"] >= active_entry["name_x0"] - 20:
        return True

    return abs(line["x0"] - active_entry.get("code_x0", -1000)) <= 8


def should_append_continuation_to_qualifier_name(
    current_name: str,
    continuation_text: str,
) -> bool:
    """
    Decide whether continuation should extend the qualifier name
    or become bracketed note.

    Example:
        Blanket Order/Estimated Quantities (Not firm
        Commitment)

    should become:
        Blanket Order/Estimated Quantities (Not firm Commitment)

    But:
        Purchase Order
        Procurement instrument within the small purchasing threshold

    should become:
        Purchase Order [Procurement instrument within the small purchasing threshold]
    """

    current_name = clean_line(current_name)
    continuation_text = clean_line(continuation_text)

    open_parens = current_name.count("(")
    close_parens = current_name.count(")")

    if open_parens > close_parens:
        return True

    if current_name.endswith((",", "-", "/", "and", "or")):
        return True

    return False


def is_description_sentence(text: str) -> bool:
    """
    Detect sentence-like explanatory text.

    This helps avoid false qualifiers like:
        3M requires the Currency Code...
        FOB07 field contains...
    """

    text = clean_line(text)

    sentence_starts = (
        "requires ",
        "will ",
        "field ",
        "standard",
        "specifying ",
        "identifying ",
        "contains ",
        "free-form ",
    )

    lowered = text.lower()

    return lowered.startswith(sentence_starts)


def extract_description(
    detail_lines: list[dict[str, Any]],
    consumed_indexes: set[int],
) -> str:
    """
    Extract description/explanatory text that is not part of the qualifier mini-table.
    """

    description_lines = []

    for index, line in enumerate(detail_lines):
        text = clean_line(line["text"])

        if not text:
            continue

        if index in consumed_indexes:
            continue

        if is_stop_section(text):
            break

        if is_code_name_header(text):
            continue

        if is_generic_note_start(text):
            text = strip_note_label_prefix(text)

            if not text:
                continue

        description_lines.append(text)

    return "\n".join(remove_duplicates(description_lines))


def extract_details(
    detail_lines: list[dict[str, Any]],
    consumed_indexes: set[int],
) -> str:
    """
    Keep leftover detail text for audit/review.
    For now this mirrors Description but can later be extended.
    """

    return extract_description(
        detail_lines=detail_lines,
        consumed_indexes=consumed_indexes,
    )


# =====================================================
# BASIC DETECTION HELPERS
# =====================================================

def clean_line(value: Any) -> str:
    value = str(value or "")
    value = value.replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_col(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def normalize_element_ref(value: str) -> str:
    value = clean_line(value)

    parts = value.split()

    if (
        len(parts) == 2
        and re.match(r"^[A-Z]{1,4}\d?$", parts[0])
        and re.match(r"^\d{2,3}$", parts[1])
    ):
        return f"{parts[0]}{parts[1]}"

    return value


def is_element_ref(value: str) -> bool:
    value = clean_line(value)

    # Compact: BEG01, PO109, N101
    if re.match(r"^[A-Z]{1,4}\d{2,3}$", value):
        return True

    # Split: "BEG 01", "PO1 02", "N1 01", "TD5 05" -- the segment-id part
    # may itself end in a single digit (N1, N3, N4, N9, PO1, TD5, ...).
    parts = value.split()

    if (
        len(parts) == 2
        and re.match(r"^[A-Z]{1,4}\d?$", parts[0])
        and re.match(r"^\d{2,3}$", parts[1])
    ):
        return True

    return False


def is_element_number(value: str) -> bool:
    value = clean_line(value)
    return bool(re.match(r"^[A-Z]?\d{1,5}$", value))


def is_leading_status(value: str) -> bool:
    value = clean_line(value)

    return value in {
        "M",
        "O",
        "C",
        "X",
        "N",
        "Required",
        "Optional",
        "Mandatory",
        "Must Use",
        "Not Used",
        "Rec",
        "Rec.",
        "Not Rec",
        "Not Rec.",
    }


def is_req_code(value: str) -> bool:
    return clean_line(value) in {"M", "O", "C", "X", "N"}


def is_data_type(value: str) -> bool:
    value = clean_line(value)

    return bool(re.match(r"^[A-Z][A-Z0-9]{0,3}$", value))


def normalize_min_max(value: str) -> str:
    value = clean_line(value)
    return re.sub(r"\s*/\s*", "/", value)


def is_qualifier_code(value: str) -> bool:
    """
    Qualifier codes are usually uppercase letters/numbers:
        BT, BY, ST, SU, 00, 06, C310, F050, ZZZ

    But we must exclude attribute tokens and common false positives.
    """

    value = clean_line(value)

    if value in {"M", "O", "C", "X", "N", "ID", "AN", "DT", "TM", "R", "R0", "N0"}:
        return False

    if not re.match(r"^[A-Z0-9]{1,8}$", value):
        return False

    return True


def is_code_name_header(text: str) -> bool:
    norm = normalize_col(text)

    return norm in {
        "code",
        "name",
        "code_name",
        "valid_codes",
        "qualifier",
        "qualifiers",
    }


def is_note_or_description_start(text: str) -> bool:
    text = clean_line(text)

    return bool(
        text.startswith("Description:")
        or text.startswith("Notes:")
        or text.startswith("Note:")
        or text.startswith("Comments:")
        or text.startswith("Comment:")
    )


def is_generic_note_start(text: str) -> bool:
    text = clean_line(text)

    return bool(
        re.match(
            r"^[A-Za-z0-9&/ .'\-]{0,60}\b(Note|Notes|Comment|Comments)(?:\s*\d+)?\s*:",
            text,
            flags=re.IGNORECASE,
        )
    )


def strip_note_label_prefix(text: str) -> str:
    """
    Strip a leading 'Note:' / 'Comment:' style label, keeping the rest
    of the line's actual content.
    """

    text = clean_line(text)

    match = re.match(
        r"^[A-Za-z0-9&/ .'\-]{0,60}\b(Note|Notes|Comment|Comments)(?:\s*\d+)?\s*:\s*",
        text,
        flags=re.IGNORECASE,
    )

    if match:
        return clean_line(text[match.end():])

    return text


def is_qualifier_like_line(text: str) -> bool:
    text = clean_line(text)

    pair = parse_qualifier_pair(text)

    return pair is not None


def is_stop_section(text: str) -> bool:
    text = clean_line(text)

    return any(text.startswith(prefix) for prefix in STOP_SECTION_PREFIXES)


def remove_duplicates(values: list[str]) -> list[str]:
    unique = []
    seen = set()

    for value in values:
        if value not in seen:
            unique.append(value)
            seen.add(value)

    return unique
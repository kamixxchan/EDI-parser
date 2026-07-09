import re
from typing import Any

import pandas as pd


def clean_text(value: Any) -> str:
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).lower())


def table_cells_to_matrix(table: dict[str, Any]) -> list[list[str]]:
    """
    Convert Azure table cells into a row/column matrix.
    """

    row_count = int(table.get("row_count") or 0)
    column_count = int(table.get("column_count") or 0)

    matrix = [["" for _ in range(column_count)] for _ in range(row_count)]

    for cell in table.get("cells", []):
        row_index = cell.get("row_index")
        column_index = cell.get("column_index")

        if row_index is None or column_index is None:
            continue

        if row_index >= row_count or column_index >= column_count:
            continue

        matrix[row_index][column_index] = clean_text(cell.get("content", ""))

    return matrix


def find_header_row(
    matrix: list[list[str]],
    expected_columns: list[str],
) -> int | None:
    """
    Find the row that best matches the user-provided source columns.
    """

    best_index = None
    best_score = 0

    expected_normalized = [normalize(col) for col in expected_columns]

    for row_index, row in enumerate(matrix[:10]):
        row_texts = [normalize(cell) for cell in row]

        score = 0

        for expected in expected_normalized:
            if any(expected == cell or expected in cell or cell in expected for cell in row_texts):
                score += 1

        if score > best_score:
            best_score = score
            best_index = row_index

    minimum_score = max(2, len(expected_columns) // 2)

    if best_score >= minimum_score:
        return best_index

    return None


def map_header_columns(
    header_row: list[str],
    expected_columns: list[str],
) -> dict[str, int | None]:
    """
    Map user expected column names to actual Azure table column indexes.
    """

    mapping = {}

    for expected_col in expected_columns:
        expected_norm = normalize(expected_col)

        best_index = None
        best_score = 0

        for index, actual_col in enumerate(header_row):
            actual_norm = normalize(actual_col)

            if not actual_norm:
                continue

            if expected_norm == actual_norm:
                score = 3
            elif expected_norm in actual_norm or actual_norm in expected_norm:
                score = 2
            else:
                score = 0

            if score > best_score:
                best_score = score
                best_index = index

        mapping[expected_col] = best_index

    return mapping


def rows_from_azure_table(
    table: dict[str, Any],
    expected_columns: list[str],
    key_column: str,
) -> list[dict[str, Any]]:
    """
    Convert one Azure-detected table into logical records.

    Rule:
    - A new record starts when key_column has a value.
    - Empty key_column rows are continuation rows.
    - Continuation text is appended to the first large text column,
      usually Element Name.
    """

    matrix = table_cells_to_matrix(table)

    if not matrix:
        return []

    header_index = find_header_row(matrix, expected_columns)

    if header_index is None:
        return []

    header_row = matrix[header_index]
    column_map = map_header_columns(header_row, expected_columns)

    key_col_index = column_map.get(key_column)

    if key_col_index is None:
        return []

    # Usually the continuation text belongs to Element Name.
    if "Element Name" in expected_columns:
        continuation_column = "Element Name"
    else:
        continuation_column = expected_columns[min(2, len(expected_columns) - 1)]

    records = []
    current_record = None

    for raw_row in matrix[header_index + 1:]:
        row_values = {}

        for expected_col in expected_columns:
            actual_index = column_map.get(expected_col)

            if actual_index is None or actual_index >= len(raw_row):
                row_values[expected_col] = ""
            else:
                row_values[expected_col] = clean_text(raw_row[actual_index])

        key_value = row_values.get(key_column, "").strip()

        if key_value:
            if current_record is not None:
                records.append(current_record)

            current_record = {
                "values": row_values,
                "extra_text_by_column": {},
                "raw_text": " | ".join(
                    value for value in row_values.values() if value
                ),
                "confidence": 0.85,
                "needs_review": False,
            }

        else:
            if current_record is None:
                continue

            continuation_parts = [
                value for value in row_values.values()
                if value.strip()
            ]

            continuation_text = "\n".join(continuation_parts).strip()

            if continuation_text:
                previous_extra = current_record["extra_text_by_column"].get(
                    continuation_column,
                    "",
                )

                if previous_extra:
                    current_record["extra_text_by_column"][continuation_column] = (
                        previous_extra + "\n" + continuation_text
                    )
                else:
                    current_record["extra_text_by_column"][continuation_column] = (
                        continuation_text
                    )

                current_record["raw_text"] += "\n" + continuation_text

    if current_record is not None:
        records.append(current_record)

    return records


def extract_code_name_mini_table(
    text: str,
) -> tuple[str, str]:
    """
    Extract simple Code/Name rows from text.

    Example:
        Code Name
        PP Prepaid (by Seller)
        PU Pickup

    Output:
        codes: PP\nPU
        names: Prepaid (by Seller)\nPickup
    """

    lines = [line.strip() for line in clean_text(text).split("\n") if line.strip()]

    start_index = None

    for i, line in enumerate(lines):
        if normalize(line) in ["codename", "codesname"]:
            start_index = i
            break

        if "code" in normalize(line) and "name" in normalize(line):
            start_index = i
            break

    if start_index is None:
        return "", ""

    codes = []
    names = []

    for line in lines[start_index + 1:]:
        # Example: PP Prepaid (by Seller)
        parts = line.split(maxsplit=1)

        if len(parts) == 1:
            continue

        code = parts[0].strip()
        name = parts[1].strip()

        # Conservative filter: codes are usually short.
        if len(code) <= 10:
            codes.append(code)
            names.append(name)

    return "\n".join(codes), "\n".join(names)


def azure_ocr_results_to_dataframe(
    azure_results: list[dict[str, Any]],
    expected_columns: list[str],
    key_column: str,
    normalized_column_map: dict[str, str] | None = None,
    nested_table_rules: list[dict[str, Any]] | None = None,
) -> pd.DataFrame:
    """
    Convert Azure OCR/layout output into final DataFrame.
    """

    normalized_column_map = normalized_column_map or {}
    nested_table_rules = nested_table_rules or []

    output_records = []

    for result in azure_results:
        source_page = result.get("source_page")
        image_path = result.get("image_path", "")

        for table in result.get("tables", []):
            logical_rows = rows_from_azure_table(
                table=table,
                expected_columns=expected_columns,
                key_column=key_column,
            )

            for row_index, logical_row in enumerate(logical_rows):
                values = logical_row.get("values", {})
                extras = logical_row.get("extra_text_by_column", {})

                record = {
                    "source_page": source_page,
                    "source_image": image_path,
                    "row_index": row_index,
                    "raw_row_text": logical_row.get("raw_text", ""),
                    "extraction_confidence": logical_row.get("confidence", 0.0),
                    "needs_review": logical_row.get("needs_review", False),
                }

                # Raw user columns
                for col in expected_columns:
                    record[col] = values.get(col, "")

                # Normalized columns
                for source_col, normalized_col in normalized_column_map.items():
                    if normalized_col:
                        record[normalized_col] = values.get(source_col, "")

                # Extra/details columns
                for source_col, extra_text in extras.items():
                    base_col = normalized_column_map.get(source_col) or source_col
                    base_col = re.sub(r"[^a-z0-9]+", "_", base_col.lower()).strip("_")
                    record[f"{base_col}_details"] = extra_text

                    # Basic Code/Name mini-table extraction
                    codes, names = extract_code_name_mini_table(extra_text)

                    if codes or names:
                        record["code_list_code"] = codes
                        record["code_list_name"] = names

                output_records.append(record)

    return pd.DataFrame(output_records)
from typing import Any

import pandas as pd


AUDIT_COLUMNS = [
    "source_page",
    "needs_review",
    "review_reason",
]


def page_json_results_to_dataframe(
    page_results: list[dict[str, Any]],
    expected_columns: list[str],
    multiline_column: str | None = None,
) -> pd.DataFrame:
    """
    Convert per-page model JSON results into a flat DataFrame.

    Output column order:
        source_page, needs_review, review_reason,
        then expected_columns in the order the user entered them —
        except the designated multiline_column, which is replaced in
        place by Element Name / Qualifiers.

    The multiline_column's value in the model's "values" dict is expected
    to be a nested object: {"element_name": str, "qualifiers": str}.
    Every other column's value is a plain string. Descriptions/notes are
    intentionally not extracted for this parser.
    """

    records: list[dict[str, Any]] = []

    for page_result in page_results:
        source_page = page_result.get("source_page")
        rows = page_result.get("rows", []) or []

        for row in rows:
            values = row.get("values", {}) or {}

            record: dict[str, Any] = {
                "source_page": source_page,
                "needs_review": bool(row.get("needs_review", False)),
                "review_reason": str(row.get("review_reason", "")).strip(),
            }

            for col in expected_columns:
                if col == multiline_column:
                    multiline_value = values.get(col, {})

                    if not isinstance(multiline_value, dict):
                        multiline_value = {}

                    record["Element Name"] = str(
                        multiline_value.get("element_name", "")
                    ).strip()
                    record["Qualifiers"] = str(
                        multiline_value.get("qualifiers", "")
                    ).strip()
                else:
                    record[col] = str(values.get(col, "")).strip()

            records.append(record)

    output_columns = build_output_column_order(
        expected_columns=expected_columns,
        multiline_column=multiline_column,
    )

    if not records:
        return pd.DataFrame(columns=output_columns)

    df = pd.DataFrame(records)

    for col in output_columns:
        if col not in df.columns:
            df[col] = ""

    return df[output_columns]


def build_output_column_order(
    expected_columns: list[str],
    multiline_column: str | None,
) -> list[str]:
    """
    Build the final output column order: audit columns first, then the
    user's columns in the order they were entered, with the multiline
    column expanded into Element Name / Qualifiers in place.
    """

    columns = list(AUDIT_COLUMNS)

    for col in expected_columns:
        if col == multiline_column:
            columns.extend(["Element Name", "Qualifiers"])
        else:
            columns.append(col)

    return columns
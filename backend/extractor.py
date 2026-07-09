import pandas as pd

from backend.formatted1_extractor import extract_formatted1_tables_from_pdf
from backend.formatted2_extractor import extract_formatted2_tables_from_pdf
from backend.other_extractor import extract_tables_from_pdf as extract_other_tables_from_pdf


def extract_tables_from_pdf(
    pdf_path: str,
    page_start: int,
    page_end: int,
    expected_columns: list[str] | None = None,
    key_column: str | None = None,
    multiline_column: str | None = None,
    extra_user_instructions: str = "",
    api_key: str | None = None,
    model: str | None = None,
    dpi: int = 220,
    document_type: str = "Other",
    include_audit_columns: bool = True,
) -> pd.DataFrame:
    """
    Router function.

    Formatted 1 → dedicated Formatted 1 parser.
    Formatted 2 → dedicated Formatted 2 parser.
    Other → generic AI/image extractor.
    """

    if document_type == "Formatted 1":
        return extract_formatted1_tables_from_pdf(
            pdf_path=pdf_path,
            page_start=page_start,
            page_end=page_end,
            expected_columns=expected_columns or [],
            key_column=key_column,
            multiline_column=multiline_column,
            include_audit_columns=include_audit_columns,
        )

    if document_type == "Formatted 2":
        return extract_formatted2_tables_from_pdf(
            pdf_path=pdf_path,
            page_start=page_start,
            page_end=page_end,
            expected_columns=expected_columns or [],
            multiline_column=multiline_column,
            include_audit_columns=include_audit_columns,
            debug=True,
        )

    return extract_other_tables_from_pdf(
        pdf_path=pdf_path,
        page_start=page_start,
        page_end=page_end,
        expected_columns=expected_columns or [],
        key_column=key_column,
        multiline_column=multiline_column,
        extra_user_instructions=extra_user_instructions,
        api_key=api_key,
        model=model,
        dpi=dpi,
    )
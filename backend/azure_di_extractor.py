import os
from typing import Any

from dotenv import load_dotenv
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient


load_dotenv()


def analyze_image_with_azure_document_intelligence(
    image_path: str,
    page_number: int,
    endpoint: str | None = None,
    key: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """
    Analyze one page image using Azure AI Document Intelligence Layout model.

    Output is normalized into a simple Python dictionary so the rest of
    your app does not depend directly on Azure SDK objects.
    """

    endpoint = endpoint or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT")
    key = key or os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")
    model_id = model_id or os.getenv(
        "AZURE_DOCUMENT_INTELLIGENCE_MODEL",
        "prebuilt-layout",
    )

    if not endpoint:
        raise ValueError("Missing AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT in .env")

    if not key:
        raise ValueError("Missing AZURE_DOCUMENT_INTELLIGENCE_KEY in .env")

    client = DocumentIntelligenceClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key),
    )

    with open(image_path, "rb") as image_file:
        poller = client.begin_analyze_document(
            model_id=model_id,
            body=image_file,
            content_type="image/png",
        )

    result = poller.result()

    return azure_result_to_simple_dict(
        result=result,
        page_number=page_number,
        image_path=image_path,
    )


def azure_result_to_simple_dict(
    result: Any,
    page_number: int,
    image_path: str,
) -> dict[str, Any]:
    """
    Convert Azure Document Intelligence result into a simple structure:

    {
        "source_page": 1,
        "image_path": "...",
        "lines": [...],
        "tables": [...]
    }
    """

    output = {
        "source_page": page_number,
        "image_path": image_path,
        "lines": [],
        "tables": [],
        "warnings": [],
    }

    # Extract OCR lines
    for page in getattr(result, "pages", []) or []:
        for line in getattr(page, "lines", []) or []:
            output["lines"].append(
                {
                    "content": getattr(line, "content", ""),
                    "page_number": page_number,
                }
            )

    # Extract detected tables
    for table_index, table in enumerate(getattr(result, "tables", []) or []):
        table_data = {
            "table_index": table_index,
            "row_count": getattr(table, "row_count", 0),
            "column_count": getattr(table, "column_count", 0),
            "cells": [],
        }

        for cell in getattr(table, "cells", []) or []:
            table_data["cells"].append(
                {
                    "row_index": getattr(cell, "row_index", None),
                    "column_index": getattr(cell, "column_index", None),
                    "content": getattr(cell, "content", ""),
                    "kind": getattr(cell, "kind", ""),
                }
            )

        output["tables"].append(table_data)

    return output
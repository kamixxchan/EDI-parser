# import base64
# import json
# import os
# import tempfile
# import time
# from typing import Any

# import pandas as pd
# from dotenv import load_dotenv
# from openai import OpenAI

# from backend.pdf_utils import pdf_pages_to_images
# from backend.formatter import page_json_results_to_dataframe


# load_dotenv()


# DEFAULT_AZURE_OPENAI_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
# DEFAULT_AZURE_OPENAI_KEY_ENV = "AZURE_OPENAI_API_KEY"
# DEFAULT_AZURE_OPENAI_DEPLOYMENT_ENV = "AZURE_OPENAI_DEPLOYMENT"


# def _get_azure_openai_client(api_key: str | None = None) -> OpenAI:
#     endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
#     key = api_key or os.getenv("AZURE_OPENAI_API_KEY")

#     if not endpoint:
#         raise ValueError("Missing AZURE_OPENAI_ENDPOINT in .env")

#     if not key:
#         raise ValueError("Missing AZURE_OPENAI_API_KEY in .env")

#     # Safety cleanup:
#     # User may accidentally paste full Foundry URL ending with /openai/v1/responses.
#     endpoint = endpoint.replace("/openai/v1/responses", "")
#     endpoint = endpoint.replace("/openai/v1", "")
#     endpoint = endpoint.rstrip("/")

#     return OpenAI(
#         api_key=key,
#         base_url=f"{endpoint}/openai/v1/",
#     )


# def _image_file_to_data_url(image_path: str) -> str:
#     """
#     Convert local PNG/JPG image to base64 data URL for Azure OpenAI vision input.
#     """

#     ext = os.path.splitext(image_path)[1].lower()

#     if ext in {".jpg", ".jpeg"}:
#         mime_type = "image/jpeg"
#     elif ext == ".webp":
#         mime_type = "image/webp"
#     else:
#         mime_type = "image/png"

#     with open(image_path, "rb") as image_file:
#         encoded = base64.b64encode(image_file.read()).decode("utf-8")

#     return f"data:{mime_type};base64,{encoded}"


# def _build_values_schema(
#     expected_columns: list[str],
#     multiline_column: str | None,
# ) -> dict[str, Any]:
#     """
#     Create fixed JSON properties for user-provided columns.

#     Every column is a plain string property, except the designated
#     multiline_column, which becomes a nested object with two required
#     fields: element_name, qualifiers. This lets the model extract the
#     multi-line cell's structure directly, instead of returning an opaque
#     blob that a second pass has to re-parse.

#     Descriptions/notes are intentionally not extracted for this parser.
#     """

#     properties: dict[str, Any] = {}

#     for col in expected_columns:
#         if col == multiline_column:
#             properties[col] = {
#                 "type": "object",
#                 "additionalProperties": False,
#                 "properties": {
#                     "element_name": {
#                         "type": "string",
#                         "description": (
#                             "The element's name, normally the first line of "
#                             "this cell (e.g. 'Shipment Method of Payment'). "
#                             "Empty string if not visible."
#                         ),
#                     },
#                     "qualifiers": {
#                         "type": "string",
#                         "description": (
#                             "The qualifier code/name list found in this cell, "
#                             "formatted as 'CODE = Name' entries joined by "
#                             "'; '. If a qualifier has its own note, append it "
#                             "in square brackets, e.g. 'CODE = Name [note]'. "
#                             "Empty string if there are no qualifiers."
#                         ),
#                     },
#                 },
#                 "required": ["element_name", "qualifiers"],
#             }
#         else:
#             properties[col] = {
#                 "type": "string",
#                 "description": (
#                     f"Exact extracted value for source column '{col}'. "
#                     "Use empty string if not visible."
#                 ),
#             }

#     return properties


# def _build_response_schema(
#     expected_columns: list[str],
#     multiline_column: str | None,
# ) -> dict[str, Any]:
#     """
#     Strict schema for Azure OpenAI structured outputs.

#     The formatter in backend.formatter expects:
#         page_result["rows"][i]["values"]
#         page_result["rows"][i]["needs_review"]
#         page_result["rows"][i]["review_reason"]

#     Every value in "values" is a plain string, except the designated
#     multiline_column, whose value is a nested
#     {element_name, qualifiers} object.
#     """

#     values_schema = _build_values_schema(expected_columns, multiline_column)

#     return {
#         "type": "json_schema",
#         "json_schema": {
#             "name": "edi_other_route_extraction",
#             "strict": True,
#             "schema": {
#                 "type": "object",
#                 "additionalProperties": False,
#                 "properties": {
#                     "source_page": {"type": "integer"},
#                     "warnings": {
#                         "type": "array",
#                         "items": {"type": "string"},
#                     },
#                     "rows": {
#                         "type": "array",
#                         "items": {
#                             "type": "object",
#                             "additionalProperties": False,
#                             "properties": {
#                                 "values": {
#                                     "type": "object",
#                                     "additionalProperties": False,
#                                     "properties": values_schema,
#                                     "required": expected_columns,
#                                 },
#                                 "needs_review": {"type": "boolean"},
#                                 "review_reason": {"type": "string"},
#                             },
#                             "required": [
#                                 "values",
#                                 "needs_review",
#                                 "review_reason",
#                             ],
#                         },
#                     },
#                 },
#                 "required": ["source_page", "warnings", "rows"],
#             },
#         },
#     }


# def _build_prompt(
#     expected_columns: list[str],
#     key_column: str,
#     multiline_column: str | None,
#     extra_user_instructions: str,
# ) -> str:
#     columns_text = "\n".join(f"- {col}" for col in expected_columns)

#     multiline_instructions = ""

#     if multiline_column:
#         multiline_instructions = f"""
# Multi-line column:
# {multiline_column}

# This column usually contains, in order:
# 1. The element's name, normally on the first line.
# 2. Sometimes a description/comment/note before any qualifier list -- IGNORE this, it is not extracted.
# 3. An optional qualifier code/name list (sometimes an explicit "Code" /
#    "Name" mini-table, sometimes just code and name separated by spacing).

# For this column, instead of one flat string, return an object with two
# fields: element_name, qualifiers. Do not extract any description, comment,
# or note text -- skip straight from the element name to the qualifier list.

# Qualifier formatting rules:
# - Each qualifier becomes "CODE = Name".
# - Join multiple qualifiers with "; ".
# - Qualifier codes are usually uppercase letters and/or numbers, 1-5
#   characters (e.g. BT, BY, ST, SU, 00, 06) -- use this as guidance, not a
#   strict rule.
# - If a qualifier has its own extra note, append it in square brackets
#   right after that qualifier, e.g. "CODE = Name [note]".
# - Do not repeat the element name inside qualifiers.

# Worked examples:

# Example A (qualifiers only):
#   element_name: "Shipment Method of Payment"
#   qualifiers: "CC = Collect; DF = Defined by Buyer and Seller; PP = Prepaid (by Seller)"

# Example B (a description appears in the cell, but is ignored):
#   element_name: "Transaction Set Identifier Code"
#   qualifiers: "850 = Purchase Order"

# Example C (qualifier with its own note):
#   element_name: "Purchase Order Type Code"
#   qualifiers: "BE = Blanket Order/Estimated Quantities (Not firm Commitment) [3M uses SAP Schedule Agreements in place of blanket orders.]; CN = Consigned Order; DS = Dropship"

# Example D (explicit Code/Name mini-table in the cell):
#   element_name: "Purchase Order Type"
#   qualifiers: "RL = Release Orders; SA = Stand Alone Orders; KC = Contract Orders"

# If this column has no qualifiers at all, return an empty string for
# qualifiers.
# """

#     return f"""
# You are an extraction engine for EDI implementation guide PDF page images.

# Goal:
# Extract the main segment/element table visible on this page into structured rows.

# Source columns requested by the user:
# {columns_text}

# Key column:
# {key_column}
# A new logical record usually begins when this column has a value.
# {multiline_instructions}
# Rules:
# 1. Extract ONLY the relevant EDI table rows. Ignore page headers, footers, logos, page numbers, unrelated notes, and surrounding prose.
# 2. Use the exact requested source columns in the values object.
# 3. If a requested column is not visible or not applicable, return an empty string for that column (or empty strings for each field, if it is the multi-line column).
# 4. Preserve visible text as accurately as possible.
# 5. Do not invent qualifiers, codes, or values that are not visible on the page.
# 6. Return all rows visible on this page, even if the segment continues from or to another page.
# 7. Set needs_review to true for any row where extraction is uncertain (e.g. cut off by a page break, illegible, or ambiguous), and briefly explain why in review_reason. Otherwise leave review_reason as an empty string.
# 8. JSON must follow the provided schema.

# Additional user instructions:
# {extra_user_instructions or "None"}
# """.strip()


# def _safe_json_loads(content: str) -> dict[str, Any]:
#     """
#     Parse JSON model output. Also handles accidental markdown fences.
#     """

#     content = (content or "").strip()

#     if content.startswith("```"):
#         content = content.strip("`").strip()
#         if content.lower().startswith("json"):
#             content = content[4:].strip()

#     return json.loads(content)


# def _call_azure_gpt_for_page(
#     client: OpenAI,
#     deployment_name: str,
#     image_path: str,
#     source_page: int,
#     expected_columns: list[str],
#     key_column: str,
#     multiline_column: str | None,
#     extra_user_instructions: str,
#     max_retries: int = 2,
# ) -> dict[str, Any]:
#     """
#     Send one page image to Azure OpenAI vision model and return the raw JSON object.
#     """

#     image_data_url = _image_file_to_data_url(image_path)

#     prompt = _build_prompt(
#         expected_columns=expected_columns,
#         key_column=key_column,
#         multiline_column=multiline_column,
#         extra_user_instructions=extra_user_instructions,
#     )

#     messages = [
#         {
#             "role": "system",
#             "content": "You extract EDI PDF table data from images. Return structured JSON only.",
#         },
#         {
#             "role": "user",
#             "content": [
#                 {
#                     "type": "text",
#                     "text": f"Extract the EDI table from PDF page {source_page}.\n\n{prompt}",
#                 },
#                 {
#                     "type": "image_url",
#                     "image_url": {"url": image_data_url},
#                 },
#             ],
#         },
#     ]

#     response_format = _build_response_schema(expected_columns, multiline_column)
#     last_error: Exception | None = None

#     for attempt in range(max_retries + 1):
#         try:
#             try:
#                 completion = client.chat.completions.create(
#                     model=deployment_name,
#                     messages=messages,
#                     temperature=0,
#                     response_format=response_format,
#                     max_tokens=12000,
#                 )
#             except Exception:
#                 # Fallback for deployments that do not accept json_schema.
#                 # JSON mode guarantees valid JSON, but not full schema adherence.
#                 completion = client.chat.completions.create(
#                     model=deployment_name,
#                     messages=messages
#                     + [
#                         {
#                             "role": "user",
#                             "content": "Return a single valid JSON object. Do not use markdown.",
#                         }
#                     ],
#                     temperature=0,
#                     response_format={"type": "json_object"},
#                     max_tokens=12000,
#                 )

#             choice = completion.choices[0]

#             if choice.finish_reason == "length":
#                 raise RuntimeError(
#                     "Azure GPT response was cut off. Try fewer pages, lower DPI, or a stronger deployment."
#                 )

#             content = choice.message.content or ""
#             result = _safe_json_loads(content)
#             result["source_page"] = int(result.get("source_page") or source_page)
#             result["image_path"] = image_path

#             return result

#         except Exception as exc:
#             last_error = exc
#             time.sleep(1.5 * (attempt + 1))

#     raise RuntimeError(f"Azure GPT extraction failed for page {source_page}: {last_error}")


# def _normalize_model_result_for_formatter(page_result: dict[str, Any]) -> dict[str, Any]:
#     """
#     Apply light defensive defaults to the strict-schema model result.

#     The schema's row shape (values / needs_review / review_reason) already
#     matches what backend.formatter expects directly, so no structural
#     transformation is needed here -- only guarding against a missing or
#     malformed field.
#     """

#     normalized_rows = []

#     for row in page_result.get("rows", []) or []:
#         normalized_rows.append(
#             {
#                 "values": row.get("values", {}) or {},
#                 "needs_review": bool(row.get("needs_review", False)),
#                 "review_reason": str(row.get("review_reason", "")).strip(),
#             }
#         )

#     return {
#         "source_page": page_result.get("source_page"),
#         "image_path": page_result.get("image_path", ""),
#         "warnings": page_result.get("warnings", []) or [],
#         "rows": normalized_rows,
#     }


# def extract_tables_from_pdf(
#     pdf_path: str,
#     page_start: int,
#     page_end: int,
#     expected_columns: list[str],
#     key_column: str | None = None,
#     multiline_column: str | None = None,
#     extra_user_instructions: str = "",
#     api_key: str | None = None,
#     model: str | None = None,
#     dpi: int = 220,
# ) -> pd.DataFrame:
#     """
#     Other route: Azure OpenAI GPT vision extraction.

#     Flow:
#         PDF -> page images -> Azure GPT-4.1-mini vision -> strict JSON -> formatter -> DataFrame

#     Formatted 1 and Formatted 2 routes are not touched because they are routed
#     before this function is called in backend/extractor.py.
#     """

#     expected_columns = [col.strip() for col in expected_columns if col and col.strip()]

#     if not expected_columns:
#         raise ValueError("Please provide at least one source column.")

#     if page_start < 1:
#         raise ValueError("page_start must be 1 or greater.")

#     if page_end < page_start:
#         raise ValueError("page_end must be greater than or equal to page_start.")

#     if not key_column:
#         key_column = expected_columns[0]

#     if key_column not in expected_columns:
#         raise ValueError(f"Key column '{key_column}' must be one of the expected columns.")

#     if multiline_column and multiline_column not in expected_columns:
#         raise ValueError(f"Multi-line column '{multiline_column}' must be one of the expected columns.")

#     deployment_name = model or os.getenv(DEFAULT_AZURE_OPENAI_DEPLOYMENT_ENV)

#     if not deployment_name:
#         raise ValueError("Missing Azure OpenAI deployment name. Set AZURE_OPENAI_DEPLOYMENT in .env or pass model=...")

#     print("DEBUG AZURE_OPENAI_ENDPOINT:", os.getenv("AZURE_OPENAI_ENDPOINT"))
#     print("DEBUG AZURE_OPENAI_DEPLOYMENT:", os.getenv("AZURE_OPENAI_DEPLOYMENT"))
#     print("DEBUG model from UI:", model)
#     print("DEBUG resolved deployment:", deployment_name)

#     client = _get_azure_openai_client(api_key=api_key)

#     page_results = []

#     with tempfile.TemporaryDirectory() as tmp_dir:
#         page_images = pdf_pages_to_images(
#             pdf_path=pdf_path,
#             output_dir=tmp_dir,
#             page_start=page_start,
#             page_end=page_end,
#             dpi=dpi,
#         )

#         for page_image in page_images:
#             raw_result = _call_azure_gpt_for_page(
#                 client=client,
#                 deployment_name=deployment_name,
#                 image_path=page_image["image_path"],
#                 source_page=page_image["page_number"],
#                 expected_columns=expected_columns,
#                 key_column=key_column,
#                 multiline_column=multiline_column,
#                 extra_user_instructions=extra_user_instructions,
#             )

#             page_results.append(_normalize_model_result_for_formatter(raw_result))

#     df = page_json_results_to_dataframe(
#         page_results=page_results,
#         expected_columns=expected_columns,
#         multiline_column=multiline_column,
#     )

#     return df






















# import base64
# import json
# import os
# import tempfile
# import time
# from typing import Any

# import pandas as pd
# from dotenv import load_dotenv
# from openai import OpenAI

# from backend.pdf_utils import pdf_pages_to_images
# from backend.formatter import page_json_results_to_dataframe


# load_dotenv()


# DEFAULT_AZURE_OPENAI_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
# DEFAULT_AZURE_OPENAI_KEY_ENV = "AZURE_OPENAI_API_KEY"
# DEFAULT_AZURE_OPENAI_DEPLOYMENT_ENV = "AZURE_OPENAI_DEPLOYMENT"


# def _get_azure_openai_client(api_key: str | None = None) -> OpenAI:
#     endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
#     key = api_key or os.getenv("AZURE_OPENAI_API_KEY")

#     if not endpoint:
#         raise ValueError("Missing AZURE_OPENAI_ENDPOINT in .env")

#     if not key:
#         raise ValueError("Missing AZURE_OPENAI_API_KEY in .env")

#     # Safety cleanup:
#     # User may accidentally paste full Foundry URL ending with /openai/v1/responses.
#     endpoint = endpoint.replace("/openai/v1/responses", "")
#     endpoint = endpoint.replace("/openai/v1", "")
#     endpoint = endpoint.rstrip("/")

#     return OpenAI(
#         api_key=key,
#         base_url=f"{endpoint}/openai/v1/",
#     )


# def _image_file_to_data_url(image_path: str) -> str:
#     """
#     Convert local PNG/JPG image to base64 data URL for Azure OpenAI vision input.
#     """

#     ext = os.path.splitext(image_path)[1].lower()

#     if ext in {".jpg", ".jpeg"}:
#         mime_type = "image/jpeg"
#     elif ext == ".webp":
#         mime_type = "image/webp"
#     else:
#         mime_type = "image/png"

#     with open(image_path, "rb") as image_file:
#         encoded = base64.b64encode(image_file.read()).decode("utf-8")

#     return f"data:{mime_type};base64,{encoded}"


# def _build_values_schema(
#     expected_columns: list[str],
#     multiline_column: str | None,
# ) -> dict[str, Any]:
#     """
#     Create fixed JSON properties for user-provided columns.

#     Every column is a plain string property, except the designated
#     multiline_column, which becomes a nested object with two required
#     fields: element_name, qualifiers. This lets the model extract the
#     multi-line cell's structure directly, instead of returning an opaque
#     blob that a second pass has to re-parse.

#     Descriptions/notes are intentionally not extracted for this parser.
#     """

#     properties: dict[str, Any] = {}

#     for col in expected_columns:
#         if col == multiline_column:
#             properties[col] = {
#                 "type": "object",
#                 "additionalProperties": False,
#                 "properties": {
#                     "element_name": {
#                         "type": "string",
#                         "description": (
#                             "The element's name, normally the first line of "
#                             "this cell (e.g. 'Shipment Method of Payment'). "
#                             "Empty string if not visible."
#                         ),
#                     },
#                     "qualifiers": {
#                         "type": "string",
#                         "description": (
#                             "The qualifier code/name list found in this cell, "
#                             "formatted as 'CODE = Name' entries joined by "
#                             "'; '. If a qualifier has its own note, append it "
#                             "in square brackets, e.g. 'CODE = Name [note]'. "
#                             "Empty string if there are no qualifiers."
#                         ),
#                     },
#                 },
#                 "required": ["element_name", "qualifiers"],
#             }
#         else:
#             properties[col] = {
#                 "type": "string",
#                 "description": (
#                     f"Exact extracted value for source column '{col}'. "
#                     "Use empty string if not visible."
#                 ),
#             }

#     return properties


# def _build_response_schema(
#     expected_columns: list[str],
#     multiline_column: str | None,
# ) -> dict[str, Any]:
#     """
#     Strict schema for Azure OpenAI structured outputs.

#     The formatter in backend.formatter expects:
#         page_result["rows"][i]["values"]
#         page_result["rows"][i]["needs_review"]
#         page_result["rows"][i]["review_reason"]

#     Every value in "values" is a plain string, except the designated
#     multiline_column, whose value is a nested
#     {element_name, qualifiers} object.
#     """

#     values_schema = _build_values_schema(expected_columns, multiline_column)

#     return {
#         "type": "json_schema",
#         "json_schema": {
#             "name": "edi_other_route_extraction",
#             "strict": True,
#             "schema": {
#                 "type": "object",
#                 "additionalProperties": False,
#                 "properties": {
#                     "source_page": {"type": "integer"},
#                     "warnings": {
#                         "type": "array",
#                         "items": {"type": "string"},
#                     },
#                     "rows": {
#                         "type": "array",
#                         "items": {
#                             "type": "object",
#                             "additionalProperties": False,
#                             "properties": {
#                                 "values": {
#                                     "type": "object",
#                                     "additionalProperties": False,
#                                     "properties": values_schema,
#                                     "required": expected_columns,
#                                 },
#                                 "needs_review": {"type": "boolean"},
#                                 "review_reason": {"type": "string"},
#                             },
#                             "required": [
#                                 "values",
#                                 "needs_review",
#                                 "review_reason",
#                             ],
#                         },
#                     },
#                 },
#                 "required": ["source_page", "warnings", "rows"],
#             },
#         },
#     }


# def _build_prompt(
#     expected_columns: list[str],
#     key_column: str,
#     multiline_column: str | None,
#     extra_user_instructions: str,
# ) -> str:
#     columns_text = "\n".join(f"- {col}" for col in expected_columns)

#     multiline_instructions = ""

#     if multiline_column:
#         multiline_instructions = f"""
# Multi-line column:
# {multiline_column}

# This column usually contains, in order:
# 1. The element's name, normally on the first line.
# 2. Sometimes a description/comment/note before any qualifier list -- IGNORE this, it is not extracted.
# 3. An optional qualifier code/name list (sometimes an explicit "Code" /
#    "Name" mini-table, sometimes just code and name separated by spacing).

# For this column, instead of one flat string, return an object with two
# fields: element_name, qualifiers. Do not extract any description, comment,
# or note text -- skip straight from the element name to the qualifier list.

# Qualifier formatting rules:
# - Each qualifier becomes "CODE = Name".
# - Join multiple qualifiers with "; ".
# - Qualifier codes are usually uppercase letters and/or numbers, 1-5
#   characters (e.g. BT, BY, ST, SU, 00, 06) -- use this as guidance, not a
#   strict rule.
# - If a qualifier has its own extra note, append it in square brackets
#   right after that qualifier, e.g. "CODE = Name [note]".
# - Do not repeat the element name inside qualifiers.

# Worked examples:

# Example A (qualifiers only):
#   element_name: "Shipment Method of Payment"
#   qualifiers: "CC = Collect; DF = Defined by Buyer and Seller; PP = Prepaid (by Seller)"

# Example B (a description appears in the cell, but is ignored):
#   element_name: "Transaction Set Identifier Code"
#   qualifiers: "850 = Purchase Order"

# Example C (qualifier with its own note):
#   element_name: "Purchase Order Type Code"
#   qualifiers: "BE = Blanket Order/Estimated Quantities (Not firm Commitment) [3M uses SAP Schedule Agreements in place of blanket orders.]; CN = Consigned Order; DS = Dropship"

# Example D (explicit Code/Name mini-table in the cell):
#   element_name: "Purchase Order Type"
#   qualifiers: "RL = Release Orders; SA = Stand Alone Orders; KC = Contract Orders"

# If this column has no qualifiers at all, return an empty string for
# qualifiers.
# """

#     return f"""
# You are an extraction engine for EDI implementation guide PDF page images.

# Goal:
# Extract the main segment/element table visible on this page into structured rows.

# Source columns requested by the user:
# {columns_text}

# Key column:
# {key_column}
# A new logical record usually begins when this column has a value.
# {multiline_instructions}
# Rules:
# 1. Extract ONLY the relevant EDI table rows. Ignore page headers, footers, logos, page numbers, unrelated notes, and surrounding prose.
# 2. Use the exact requested source columns in the values object.
# 3. If a requested column is not visible or not applicable, return an empty string for that column (or empty strings for each field, if it is the multi-line column).
# 4. Preserve visible text as accurately as possible. If a cell visually spans multiple lines or contains a stacked list of values (e.g. several "CODE NAME" rows in one cell), preserve that as multiple lines in the string value, joined with "\n" in the same order they appear -- do not collapse them onto one line or reformat them.
# 5. Do not invent qualifiers, codes, or values that are not visible on the page.
# 6. Return all rows visible on this page, even if the segment continues from or to another page.
# 7. Set needs_review to true for any row where extraction is uncertain (e.g. cut off by a page break, illegible, or ambiguous), and briefly explain why in review_reason. Otherwise leave review_reason as an empty string.
# 8. JSON must follow the provided schema.

# Additional user instructions:
# {extra_user_instructions or "None"}
# """.strip()


# def _safe_json_loads(content: str) -> dict[str, Any]:
#     """
#     Parse JSON model output. Also handles accidental markdown fences.
#     """

#     content = (content or "").strip()

#     if content.startswith("```"):
#         content = content.strip("`").strip()
#         if content.lower().startswith("json"):
#             content = content[4:].strip()

#     return json.loads(content)


# def _call_azure_gpt_for_page(
#     client: OpenAI,
#     deployment_name: str,
#     image_path: str,
#     source_page: int,
#     expected_columns: list[str],
#     key_column: str,
#     multiline_column: str | None,
#     extra_user_instructions: str,
#     max_retries: int = 2,
# ) -> dict[str, Any]:
#     """
#     Send one page image to Azure OpenAI vision model and return the raw JSON object.
#     """

#     image_data_url = _image_file_to_data_url(image_path)

#     prompt = _build_prompt(
#         expected_columns=expected_columns,
#         key_column=key_column,
#         multiline_column=multiline_column,
#         extra_user_instructions=extra_user_instructions,
#     )

#     messages = [
#         {
#             "role": "system",
#             "content": "You extract EDI PDF table data from images. Return structured JSON only.",
#         },
#         {
#             "role": "user",
#             "content": [
#                 {
#                     "type": "text",
#                     "text": f"Extract the EDI table from PDF page {source_page}.\n\n{prompt}",
#                 },
#                 {
#                     "type": "image_url",
#                     "image_url": {"url": image_data_url},
#                 },
#             ],
#         },
#     ]

#     response_format = _build_response_schema(expected_columns, multiline_column)
#     last_error: Exception | None = None

#     for attempt in range(max_retries + 1):
#         try:
#             try:
#                 completion = client.chat.completions.create(
#                     model=deployment_name,
#                     messages=messages,
#                     temperature=0,
#                     response_format=response_format,
#                     max_tokens=12000,
#                 )
#             except Exception:
#                 # Fallback for deployments that do not accept json_schema.
#                 # JSON mode guarantees valid JSON, but not full schema adherence.
#                 completion = client.chat.completions.create(
#                     model=deployment_name,
#                     messages=messages
#                     + [
#                         {
#                             "role": "user",
#                             "content": "Return a single valid JSON object. Do not use markdown.",
#                         }
#                     ],
#                     temperature=0,
#                     response_format={"type": "json_object"},
#                     max_tokens=12000,
#                 )

#             choice = completion.choices[0]

#             if choice.finish_reason == "length":
#                 raise RuntimeError(
#                     "Azure GPT response was cut off. Try fewer pages, lower DPI, or a stronger deployment."
#                 )

#             content = choice.message.content or ""
#             result = _safe_json_loads(content)
#             result["source_page"] = int(result.get("source_page") or source_page)
#             result["image_path"] = image_path

#             return result

#         except Exception as exc:
#             last_error = exc
#             time.sleep(1.5 * (attempt + 1))

#     raise RuntimeError(f"Azure GPT extraction failed for page {source_page}: {last_error}")


# def _normalize_model_result_for_formatter(page_result: dict[str, Any]) -> dict[str, Any]:
#     """
#     Apply light defensive defaults to the strict-schema model result.

#     The schema's row shape (values / needs_review / review_reason) already
#     matches what backend.formatter expects directly, so no structural
#     transformation is needed here -- only guarding against a missing or
#     malformed field.
#     """

#     normalized_rows = []

#     for row in page_result.get("rows", []) or []:
#         normalized_rows.append(
#             {
#                 "values": row.get("values", {}) or {},
#                 "needs_review": bool(row.get("needs_review", False)),
#                 "review_reason": str(row.get("review_reason", "")).strip(),
#             }
#         )

#     return {
#         "source_page": page_result.get("source_page"),
#         "image_path": page_result.get("image_path", ""),
#         "warnings": page_result.get("warnings", []) or [],
#         "rows": normalized_rows,
#     }


# def extract_tables_from_pdf(
#     pdf_path: str,
#     page_start: int,
#     page_end: int,
#     expected_columns: list[str],
#     key_column: str | None = None,
#     multiline_column: str | None = None,
#     extra_user_instructions: str = "",
#     api_key: str | None = None,
#     model: str | None = None,
#     dpi: int = 220,
# ) -> pd.DataFrame:
#     """
#     Other route: Azure OpenAI GPT vision extraction.

#     Flow:
#         PDF -> page images -> Azure GPT-4.1-mini vision -> strict JSON -> formatter -> DataFrame

#     Formatted 1 and Formatted 2 routes are not touched because they are routed
#     before this function is called in backend/extractor.py.
#     """

#     expected_columns = [col.strip() for col in expected_columns if col and col.strip()]

#     if not expected_columns:
#         raise ValueError("Please provide at least one source column.")

#     if page_start < 1:
#         raise ValueError("page_start must be 1 or greater.")

#     if page_end < page_start:
#         raise ValueError("page_end must be greater than or equal to page_start.")

#     if not key_column:
#         key_column = expected_columns[0]

#     if key_column not in expected_columns:
#         raise ValueError(f"Key column '{key_column}' must be one of the expected columns.")

#     if multiline_column and multiline_column not in expected_columns:
#         raise ValueError(f"Multi-line column '{multiline_column}' must be one of the expected columns.")

#     deployment_name = model or os.getenv(DEFAULT_AZURE_OPENAI_DEPLOYMENT_ENV)

#     if not deployment_name:
#         raise ValueError("Missing Azure OpenAI deployment name. Set AZURE_OPENAI_DEPLOYMENT in .env or pass model=...")

#     print("DEBUG AZURE_OPENAI_ENDPOINT:", os.getenv("AZURE_OPENAI_ENDPOINT"))
#     print("DEBUG AZURE_OPENAI_DEPLOYMENT:", os.getenv("AZURE_OPENAI_DEPLOYMENT"))
#     print("DEBUG model from UI:", model)
#     print("DEBUG resolved deployment:", deployment_name)

#     client = _get_azure_openai_client(api_key=api_key)

#     page_results = []

#     with tempfile.TemporaryDirectory() as tmp_dir:
#         page_images = pdf_pages_to_images(
#             pdf_path=pdf_path,
#             output_dir=tmp_dir,
#             page_start=page_start,
#             page_end=page_end,
#             dpi=dpi,
#         )

#         for page_image in page_images:
#             raw_result = _call_azure_gpt_for_page(
#                 client=client,
#                 deployment_name=deployment_name,
#                 image_path=page_image["image_path"],
#                 source_page=page_image["page_number"],
#                 expected_columns=expected_columns,
#                 key_column=key_column,
#                 multiline_column=multiline_column,
#                 extra_user_instructions=extra_user_instructions,
#             )

#             page_results.append(_normalize_model_result_for_formatter(raw_result))

#     df = page_json_results_to_dataframe(
#         page_results=page_results,
#         expected_columns=expected_columns,
#         multiline_column=multiline_column,
#     )

#     return df













import base64
import json
import os
import tempfile
import time
from typing import Any

import pandas as pd
from dotenv import load_dotenv
from openai import OpenAI

from backend.pdf_utils import pdf_pages_to_images
from backend.formatter import page_json_results_to_dataframe


load_dotenv()


DEFAULT_AZURE_OPENAI_ENDPOINT_ENV = "AZURE_OPENAI_ENDPOINT"
DEFAULT_AZURE_OPENAI_KEY_ENV = "AZURE_OPENAI_API_KEY"
DEFAULT_AZURE_OPENAI_DEPLOYMENT_ENV = "AZURE_OPENAI_DEPLOYMENT"


def _get_azure_openai_client(api_key: str | None = None) -> OpenAI:
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip().rstrip("/")
    key = api_key or os.getenv("AZURE_OPENAI_API_KEY")

    if not endpoint:
        raise ValueError("Missing AZURE_OPENAI_ENDPOINT in .env")

    if not key:
        raise ValueError("Missing AZURE_OPENAI_API_KEY in .env")

    # Safety cleanup:
    # User may accidentally paste full Foundry URL ending with /openai/v1/responses.
    endpoint = endpoint.replace("/openai/v1/responses", "")
    endpoint = endpoint.replace("/openai/v1", "")
    endpoint = endpoint.rstrip("/")

    return OpenAI(
        api_key=key,
        base_url=f"{endpoint}/openai/v1/",
    )


def _image_file_to_data_url(image_path: str) -> str:
    """
    Convert local PNG/JPG image to base64 data URL for Azure OpenAI vision input.
    """

    ext = os.path.splitext(image_path)[1].lower()

    if ext in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif ext == ".webp":
        mime_type = "image/webp"
    else:
        mime_type = "image/png"

    with open(image_path, "rb") as image_file:
        encoded = base64.b64encode(image_file.read()).decode("utf-8")

    return f"data:{mime_type};base64,{encoded}"


def _build_values_schema(
    expected_columns: list[str],
    multiline_column: str | None,
) -> dict[str, Any]:
    """
    Create fixed JSON properties for user-provided columns.

    Every column is a plain string property, except the designated
    multiline_column, which becomes a nested object with two required
    fields: element_name, qualifiers. This lets the model extract the
    multi-line cell's structure directly, instead of returning an opaque
    blob that a second pass has to re-parse.

    Descriptions/notes are intentionally not extracted for this parser.
    """

    properties: dict[str, Any] = {}

    for col in expected_columns:
        if col == multiline_column:
            properties[col] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "element_name": {
                        "type": "string",
                        "description": (
                            "The element's name, normally the first line of "
                            "this cell (e.g. 'Shipment Method of Payment'). "
                            "Empty string if not visible."
                        ),
                    },
                    "qualifiers": {
                        "type": "string",
                        "description": (
                            "The qualifier code/name list found in this cell, "
                            "formatted as 'CODE = Name' entries joined by "
                            "newlines (one qualifier per line). If a qualifier "
                            "has its own note, append it in square brackets, "
                            "e.g. 'CODE = Name [note]'. "
                            "Empty string if there are no qualifiers."
                        ),
                    },
                },
                "required": ["element_name", "qualifiers"],
            }
        else:
            properties[col] = {
                "type": "string",
                "description": (
                    f"Exact extracted value for source column '{col}'. "
                    "Use empty string if not visible."
                ),
            }

    return properties


def _build_response_schema(
    expected_columns: list[str],
    multiline_column: str | None,
) -> dict[str, Any]:
    """
    Strict schema for Azure OpenAI structured outputs.

    The formatter in backend.formatter expects:
        page_result["rows"][i]["values"]
        page_result["rows"][i]["needs_review"]
        page_result["rows"][i]["review_reason"]

    Every value in "values" is a plain string, except the designated
    multiline_column, whose value is a nested
    {element_name, qualifiers} object.
    """

    values_schema = _build_values_schema(expected_columns, multiline_column)

    return {
        "type": "json_schema",
        "json_schema": {
            "name": "edi_other_route_extraction",
            "strict": True,
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_page": {"type": "integer"},
                    "warnings": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "values": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": values_schema,
                                    "required": expected_columns,
                                },
                                "needs_review": {"type": "boolean"},
                                "review_reason": {"type": "string"},
                            },
                            "required": [
                                "values",
                                "needs_review",
                                "review_reason",
                            ],
                        },
                    },
                },
                "required": ["source_page", "warnings", "rows"],
            },
        },
    }


def _build_prompt(
    expected_columns: list[str],
    key_column: str,
    multiline_column: str | None,
    extra_user_instructions: str,
) -> str:
    columns_text = "\n".join(f"- {col}" for col in expected_columns)

    multiline_instructions = ""

    if multiline_column:
        multiline_instructions = f"""
Multi-line column:
{multiline_column}

This column usually contains, in order:
1. The element's name, normally on the first line.
2. Sometimes a description/comment/note before any qualifier list -- IGNORE this, it is not extracted.
3. An optional qualifier code/name list (sometimes an explicit "Code" /
   "Name" mini-table, sometimes just code and name separated by spacing).

For this column, instead of one flat string, return an object with two
fields: element_name, qualifiers. Do not extract any description, comment,
or note text -- skip straight from the element name to the qualifier list.

Qualifier formatting rules:
- Each qualifier becomes "CODE = Name".
- Join multiple qualifiers with a newline (each qualifier on its own line).
- Qualifier codes are usually uppercase letters and/or numbers, 1-5
  characters (e.g. BT, BY, ST, SU, 00, 06) -- use this as guidance, not a
  strict rule.
- If a qualifier has its own extra note, append it in square brackets
  right after that qualifier, e.g. "CODE = Name [note]".
- Do not repeat the element name inside qualifiers.

Worked examples:

Example A (qualifiers only):
  element_name: "Shipment Method of Payment"
  qualifiers: "CC = Collect\\nDF = Defined by Buyer and Seller\\nPP = Prepaid (by Seller)"

Example B (a description appears in the cell, but is ignored):
  element_name: "Transaction Set Identifier Code"
  qualifiers: "850 = Purchase Order"

Example C (qualifier with its own note):
  element_name: "Purchase Order Type Code"
  qualifiers: "BE = Blanket Order/Estimated Quantities (Not firm Commitment) [3M uses SAP Schedule Agreements in place of blanket orders.]\\nCN = Consigned Order\\nDS = Dropship"

Example D (explicit Code/Name mini-table in the cell):
  element_name: "Purchase Order Type"
  qualifiers: "RL = Release Orders\\nSA = Stand Alone Orders\\nKC = Contract Orders"

If this column has no qualifiers at all, return an empty string for
qualifiers.
"""

    return f"""
You are an extraction engine for EDI implementation guide PDF page images.

Goal:
Extract the main segment/element table visible on this page into structured rows.

Additional user instructions (these take priority over the numbered rules
below whenever the two conflict -- e.g. if a rule below says to include
everything and this section says to skip or start from a particular
segment, follow this section):
{extra_user_instructions or "None"}

Source columns requested by the user:
{columns_text}

Key column:
{key_column}
A new logical record usually begins when this column has a value.
{multiline_instructions}
Rules:
1. Extract ONLY the relevant EDI table rows. Ignore page headers, footers, logos, page numbers, unrelated notes, and surrounding prose.
2. Use the exact requested source columns in the values object.
3. If a requested column is not visible or not applicable, return an empty string for that column (or empty strings for each field, if it is the multi-line column).
4. Preserve visible text as accurately as possible. If a cell visually spans multiple lines or contains a stacked list of values (e.g. several "CODE NAME" rows in one cell), preserve that as multiple lines in the string value, joined with "\n" in the same order they appear -- do not collapse them onto one line or reformat them.
5. Do not invent qualifiers, codes, or values that are not visible on the page.
6. By default, return all rows visible on this page, even if the segment continues from or to another page -- UNLESS the additional user instructions above say to skip, exclude, or start from a particular segment/row, in which case follow those instructions instead for this page.
7. Set needs_review to true for any row where extraction is uncertain (e.g. cut off by a page break, illegible, or ambiguous), and briefly explain why in review_reason. Otherwise leave review_reason as an empty string.
8. JSON must follow the provided schema.
""".strip()


def _safe_json_loads(content: str) -> dict[str, Any]:
    """
    Parse JSON model output. Also handles accidental markdown fences.
    """

    content = (content or "").strip()

    if content.startswith("```"):
        content = content.strip("`").strip()
        if content.lower().startswith("json"):
            content = content[4:].strip()

    return json.loads(content)


def _call_azure_gpt_for_page(
    client: OpenAI,
    deployment_name: str,
    image_path: str,
    source_page: int,
    expected_columns: list[str],
    key_column: str,
    multiline_column: str | None,
    extra_user_instructions: str,
    max_retries: int = 2,
) -> dict[str, Any]:
    """
    Send one page image to Azure OpenAI vision model and return the raw JSON object.
    """

    image_data_url = _image_file_to_data_url(image_path)

    prompt = _build_prompt(
        expected_columns=expected_columns,
        key_column=key_column,
        multiline_column=multiline_column,
        extra_user_instructions=extra_user_instructions,
    )

    messages = [
        {
            "role": "system",
            "content": "You extract EDI PDF table data from images. Return structured JSON only.",
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Extract the EDI table from PDF page {source_page}.\n\n{prompt}",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": image_data_url},
                },
            ],
        },
    ]

    response_format = _build_response_schema(expected_columns, multiline_column)
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            try:
                completion = client.chat.completions.create(
                    model=deployment_name,
                    messages=messages,
                    temperature=0,
                    response_format=response_format,
                    max_tokens=12000,
                )
            except Exception:
                # Fallback for deployments that do not accept json_schema.
                # JSON mode guarantees valid JSON, but not full schema adherence.
                completion = client.chat.completions.create(
                    model=deployment_name,
                    messages=messages
                    + [
                        {
                            "role": "user",
                            "content": "Return a single valid JSON object. Do not use markdown.",
                        }
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                    max_tokens=12000,
                )

            choice = completion.choices[0]

            if choice.finish_reason == "length":
                raise RuntimeError(
                    "Azure GPT response was cut off. Try fewer pages, lower DPI, or a stronger deployment."
                )

            content = choice.message.content or ""
            result = _safe_json_loads(content)
            result["source_page"] = int(result.get("source_page") or source_page)
            result["image_path"] = image_path

            return result

        except Exception as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))

    raise RuntimeError(f"Azure GPT extraction failed for page {source_page}: {last_error}")


def _normalize_model_result_for_formatter(page_result: dict[str, Any]) -> dict[str, Any]:
    """
    Apply light defensive defaults to the strict-schema model result.

    The schema's row shape (values / needs_review / review_reason) already
    matches what backend.formatter expects directly, so no structural
    transformation is needed here -- only guarding against a missing or
    malformed field.
    """

    normalized_rows = []

    for row in page_result.get("rows", []) or []:
        normalized_rows.append(
            {
                "values": row.get("values", {}) or {},
                "needs_review": bool(row.get("needs_review", False)),
                "review_reason": str(row.get("review_reason", "")).strip(),
            }
        )

    return {
        "source_page": page_result.get("source_page"),
        "image_path": page_result.get("image_path", ""),
        "warnings": page_result.get("warnings", []) or [],
        "rows": normalized_rows,
    }


def extract_tables_from_pdf(
    pdf_path: str,
    page_start: int,
    page_end: int,
    expected_columns: list[str],
    key_column: str | None = None,
    multiline_column: str | None = None,
    extra_user_instructions: str = "",
    api_key: str | None = None,
    model: str | None = None,
    dpi: int = 220,
) -> pd.DataFrame:
    """
    Other route: Azure OpenAI GPT vision extraction.

    Flow:
        PDF -> page images -> Azure GPT-4.1-mini vision -> strict JSON -> formatter -> DataFrame

    Formatted 1 and Formatted 2 routes are not touched because they are routed
    before this function is called in backend/extractor.py.
    """

    expected_columns = [col.strip() for col in expected_columns if col and col.strip()]

    if not expected_columns:
        raise ValueError("Please provide at least one source column.")

    if page_start < 1:
        raise ValueError("page_start must be 1 or greater.")

    if page_end < page_start:
        raise ValueError("page_end must be greater than or equal to page_start.")

    if not key_column:
        key_column = expected_columns[0]

    if key_column not in expected_columns:
        raise ValueError(f"Key column '{key_column}' must be one of the expected columns.")

    if multiline_column and multiline_column not in expected_columns:
        raise ValueError(f"Multi-line column '{multiline_column}' must be one of the expected columns.")

    deployment_name = model or os.getenv(DEFAULT_AZURE_OPENAI_DEPLOYMENT_ENV)

    if not deployment_name:
        raise ValueError("Missing Azure OpenAI deployment name. Set AZURE_OPENAI_DEPLOYMENT in .env or pass model=...")

    print("DEBUG AZURE_OPENAI_ENDPOINT:", os.getenv("AZURE_OPENAI_ENDPOINT"))
    print("DEBUG AZURE_OPENAI_DEPLOYMENT:", os.getenv("AZURE_OPENAI_DEPLOYMENT"))
    print("DEBUG model from UI:", model)
    print("DEBUG resolved deployment:", deployment_name)

    client = _get_azure_openai_client(api_key=api_key)

    page_results = []

    with tempfile.TemporaryDirectory() as tmp_dir:
        page_images = pdf_pages_to_images(
            pdf_path=pdf_path,
            output_dir=tmp_dir,
            page_start=page_start,
            page_end=page_end,
            dpi=dpi,
        )

        for page_image in page_images:
            raw_result = _call_azure_gpt_for_page(
                client=client,
                deployment_name=deployment_name,
                image_path=page_image["image_path"],
                source_page=page_image["page_number"],
                expected_columns=expected_columns,
                key_column=key_column,
                multiline_column=multiline_column,
                extra_user_instructions=extra_user_instructions,
            )

            page_results.append(_normalize_model_result_for_formatter(raw_result))

    df = page_json_results_to_dataframe(
        page_results=page_results,
        expected_columns=expected_columns,
        multiline_column=multiline_column,
    )

    return df
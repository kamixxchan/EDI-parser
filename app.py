# """
# EDI 850 PDF Extractor — Streamlit UI

# This file only contains UI/UX/presentation code. The extraction and export
# logic still lives in `backend/extractor.py` and `backend/export_utils.py` and
# is called exactly the same way it always was — nothing about how PDFs are
# parsed, mapped, or exported has changed.
# """

# import base64
# import threading
# import time
# import tempfile
# from pathlib import Path

# import pandas as pd
# import streamlit as st

# from backend.export_utils import dataframe_to_excel_bytes_with_report
# from backend.extractor import extract_tables_from_pdf
# from backend.report_utils import build_extraction_report


# # ---------------------------------------------------------------------------
# # Configuration
# # ---------------------------------------------------------------------------

# # Update these paths to point at the sample PDFs you ship with the app.
# # They are only used to render the inline "Sample" preview panels — nothing
# # else depends on them, and a missing file just hides the preview.
# SAMPLE_PDF_PATHS = {
#     "Formatted 1": "assets/samples/formatted_1_sample.pdf",
#     "Formatted 2": "assets/samples/formatted_2_sample.pdf",
# }

# # Status messages cycled through while extraction runs in the background.
# STATUS_STEPS = [
#     "Preparing uploaded PDF",
#     "Reading selected pages",
#     "Detecting format/layout",
#     "Extracting segment tables",
#     "Splitting merged columns",
#     "Cleaning element numbers and names",
#     "Detecting Loop and Level",
#     "Generating final table",
# ]

# # Default columns for Formatted 1's "Table columns from the PDF" text area.
# DEFAULT_FORMATTED1_COLUMNS = """Ref
# Id
# Element Name
# Req
# Type
# Min/Max
# Usage"""

# # Default columns for Formatted 2's "Table columns from the PDF" text area.
# DEFAULT_FORMATTED2_COLUMNS = """Ref. Des.
# Data Element
# Name
# Attributes"""

# DEFAULT_OTHER_COLUMNS = """Ref
# Id
# Element Name
# Req
# Type
# Min/Max
# Usage"""

# DEFAULT_EXTRA_INSTRUCTIONS = (
#     "Extract only the element summary table. Ignore page headers, footers, "
#     "segment titles, and unrelated text."
# )

# PAGE_RANGE_HINT = (
#     "Recommended: start from the **BEG** segment page and continue until the "
#     "page before the **SE** segment."
# )

# # Fixed AI-extraction defaults for "Other" mode. There is no UI for these —
# # see the "Improve Other-mode UI" change. Edit the values here if you ever
# # need to change them.
# OTHER_MODE_DPI = 220
# OTHER_MODE_MODEL = "gpt-4.1-mini"


# # ---------------------------------------------------------------------------
# # Small data-shaping helpers (unchanged logic, just kept local to the UI)
# # ---------------------------------------------------------------------------

# def split_column_text(text: str) -> list[str]:
#     """Convert multiline input into list of columns."""
#     return [line.strip() for line in text.splitlines() if line.strip()]


# def highlight_review_rows(row):
#     """Highlight rows that need review in amber (matches --eb-warning-bg)."""
#     needs_review = bool(row.get("needs_review", False))
#     if needs_review:
#         return ["background-color: #FFF6E5"] * len(row)
#     return [""] * len(row)


# def render_extraction_report(report: dict) -> None:
#     """Render the Extraction Report as a collapsible section."""

#     with st.expander("Extraction Report", expanded=False):
#         st.markdown("###### File Information")
#         st.table(
#             pd.DataFrame(
#                 list(report["file_info"].items()), columns=["Field", "Value"]
#             ).set_index("Field")
#         )

#         st.markdown("###### Processing Summary")
#         st.table(
#             pd.DataFrame(
#                 list(report["processing_summary"].items()),
#                 columns=["Metric", "Value"],
#             ).set_index("Metric")
#         )

#         st.markdown("###### Segment Summary")
#         segment_summary = report["segment_summary"]
#         if segment_summary.empty:
#             st.caption("No segment information available for this extraction mode.")
#         else:
#             st.dataframe(segment_summary, width="stretch", hide_index=True)

#         st.markdown("###### Quality Summary")
#         st.table(
#             pd.DataFrame(
#                 list(report["quality_summary"].items()),
#                 columns=["Metric", "Value"],
#             ).set_index("Metric")
#         )

#         st.markdown("###### Extraction Confidence / Status")
#         status_info = report["status"]
#         status_style = {
#             "Excellent": st.success,
#             "Good": st.success,
#             "Moderate": st.warning,
#             "Needs Careful Review": st.error,
#             "No Data Extracted": st.error,
#         }.get(status_info["status"], st.info)
#         status_style(f"**Status: {status_info['status']}**\n\n{status_info['explanation']}")

#         st.markdown("###### Warnings and Recommendations")
#         for warning_text in report["warnings"]:
#             st.markdown(f"- {warning_text}")

#         review_reason_summary = report["review_reason_summary"]
#         if not review_reason_summary.empty:
#             st.markdown("###### Most Common Review Reasons")
#             st.dataframe(review_reason_summary, width="stretch", hide_index=True)


# # ---------------------------------------------------------------------------
# # Visual styling
# # ---------------------------------------------------------------------------

# def inject_global_css() -> None:
#     st.markdown(
#         """
#         <style>
#         @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap');

#         :root{
#             --eb-bg:#F6F7FB;
#             --eb-card:#FFFFFF;
#             --eb-border:#E6E8F0;
#             --eb-text:#11151C;
#             --eb-text-muted:#6B7280;
#             --eb-primary:#4F46E5;
#             --eb-primary-dark:#3F36C9;
#             --eb-success:#10B981;
#             --eb-warning:#B45309;
#             --eb-warning-bg:#FFF6E5;
#             --eb-warning-border:#F3D9A4;
#         }

#         html, body, [class*="css"]  { font-family: 'Inter', sans-serif; }

#         .stApp { background: var(--eb-bg); }

#         section.main > div { padding-top: 1.2rem; }

#         code, .eb-mono { font-family: 'JetBrains Mono', monospace !important; }

#         /* ---- Remove auto-generated heading anchor/link icons ----
#         Streamlit automatically turns every markdown heading (#, ##, ###,
#         ####, #####) plus st.header/st.subheader/st.title into a clickable
#         anchor and renders a chain-link icon beside it. This app doesn't use
#         deep-linking to sections, so the icon is just visual noise next to
#         every heading, section title, and label — hide it everywhere. */
#         [data-testid="stHeaderActionElements"],
#         [data-testid="stHeaderActionButton"],
#         .stHeadingActionElements{
#             display: none !important;
#         }

#         /* ---- Output table toolbar: "Columns" popover + "Download Excel" ----
#         Compact, white, light-border, rounded "toolbar button" look for both
#         the Columns popover trigger and the Download Excel button, and a
#         card-style popover panel (white, bordered, shadowed, fixed width and
#         height) for the show/hide column list. Scoped to the toolbar row via
#         the "st-key-output_table_toolbar" hook so nothing else in the app is
#         affected. The two buttons live directly inside that keyed container
#         (no st.columns nesting — that extra layout nesting is what was
#         throwing off the popover's anchor position), and are turned into a
#         single right-aligned, tightly-packed flex row here. */
#         div[class*="st-key-output_table_toolbar"] > div [data-testid="stVerticalBlock"]{
#             display:flex !important;
#             flex-direction:row !important;
#             justify-content:flex-end !important;
#             align-items:center !important;
#             gap:0.4rem !important;
#         }
#         div[class*="st-key-output_table_toolbar"] [data-testid="stElementContainer"]{
#             width:auto !important;
#             flex:0 0 auto !important;
#         }
#         div[class*="st-key-output_table_toolbar"] [data-testid="stPopover"] > div > button,
#         div[class*="st-key-output_table_toolbar"] .stDownloadButton button{
#             background:#FFFFFF !important;
#             border:1px solid var(--eb-border) !important;
#             border-radius:8px !important;
#             color:var(--eb-text) !important;
#             font-weight:600 !important;
#             font-size:0.85rem !important;
#             padding:0.35rem 0.85rem !important;
#             min-height:2.1rem !important;
#             box-shadow:none !important;
#             white-space:nowrap !important;
#         }
#         div[class*="st-key-output_table_toolbar"] [data-testid="stPopover"] > div > button:hover,
#         div[class*="st-key-output_table_toolbar"] .stDownloadButton button:hover{
#             border-color:var(--eb-primary) !important;
#             color:var(--eb-primary) !important;
#         }
#         div[data-testid="stPopoverBody"]{
#             width:300px !important;
#             max-height:420px !important;
#             overflow-y:auto !important;
#             border-radius:12px !important;
#             border:1px solid var(--eb-border) !important;
#             box-shadow:0 12px 32px rgba(17,21,28,0.14) !important;
#         }
#         .eb-colpanel-header{
#             display:flex;
#             align-items:center;
#             gap:0.55rem;
#             margin-bottom:0.65rem;
#         }
#         .eb-colpanel-icon{
#             display:flex;
#             align-items:center;
#             justify-content:center;
#             width:26px;
#             height:26px;
#             border-radius:7px;
#             background:var(--eb-bg);
#             border:1px solid var(--eb-border);
#             flex-shrink:0;
#             font-size:0.85rem;
#         }
#         .eb-colpanel-title{
#             font-weight:700;
#             font-size:0.92rem;
#             color:var(--eb-text);
#         }


#         /* ---- Header / hero ---- */
#         .eb-eyebrow{
#             font-family:'JetBrains Mono', monospace;
#             font-size:0.72rem;
#             letter-spacing:.14em;
#             text-transform:uppercase;
#             color: var(--eb-primary);
#             font-weight:600;
#             margin-bottom:.35rem;
#         }
#         .eb-title{
#             font-size:2rem;
#             font-weight:800;
#             color:var(--eb-text);
#             margin:0 0 .15rem 0;
#             letter-spacing:-0.01em;
#         }
#         .eb-subtitle{
#             color:var(--eb-text-muted);
#             font-size:0.98rem;
#             margin-bottom:0.4rem;
#         }
#         .eb-chip{
#             display:inline-block;
#             font-family:'JetBrains Mono', monospace;
#             font-size:0.74rem;
#             font-weight:600;
#             color:var(--eb-primary-dark);
#             background:#EEF0FE;
#             border:1px solid #DADCFB;
#             border-radius:999px;
#             padding:0.18rem 0.65rem;
#             margin-top:0.35rem;
#         }

#         /* ---- Stepper ---- */
#         .stepper-wrap{
#             display:flex;
#             align-items:flex-start;
#             justify-content:space-between;
#             margin: 0.9rem 0 0.4rem 0;
#             padding: 1.1rem 0.6rem 0.4rem 0.6rem;
#         }
#         .step{
#             display:flex;
#             flex-direction:column;
#             align-items:center;
#             flex:1;
#             position:relative;
#             text-align:center;
#             padding:0 6px;
#         }
#         .step-line{
#             position:absolute;
#             top:17px;
#             left:50%;
#             width:100%;
#             height:3px;
#             background:var(--eb-border);
#             z-index:1;
#             border-radius:2px;
#         }
#         .step:last-child .step-line{ display:none; }
#         .step.done .step-line{ background:var(--eb-success); }
#         .step-circle{
#             width:34px;height:34px;border-radius:50%;
#             display:flex;align-items:center;justify-content:center;
#             font-weight:700;font-size:0.82rem;
#             border:2px solid var(--eb-border);
#             background:#fff;color:var(--eb-text-muted);
#             position:relative;z-index:2;
#             transition: all .2s ease;
#         }
#         .step.done .step-circle{
#             background:var(--eb-success);
#             border-color:var(--eb-success);
#             color:#fff;
#         }
#         .step.active .step-circle{
#             border-color:var(--eb-primary);
#             color:var(--eb-primary);
#             box-shadow:0 0 0 4px rgba(79,70,229,0.14);
#             background:#fff;
#         }
#         .step-label{
#             margin-top:0.45rem;
#             font-size:0.78rem;
#             font-weight:600;
#             color:var(--eb-text);
#         }
#         .step.pending .step-label{ color: var(--eb-text-muted); }

#         /* ---- Cards ---- */
#         div[data-testid="stVerticalBlockBorderWrapper"]{
#             border-radius:16px !important;
#             border:1px solid var(--eb-border) !important;
#             background:var(--eb-card);
#             box-shadow:0 1px 2px rgba(16,24,40,0.04), 0 1px 8px rgba(16,24,40,0.03);
#         }
#         div[data-testid="stVerticalBlockBorderWrapper"] > div { padding:0.2rem 0.1rem; }

#         /* ---- Buttons ---- */
#         .stButton button{
#             border-radius:10px;
#             font-weight:600;
#             border:1px solid var(--eb-border);
#             transition: all .15s ease;
#         }
#         .stButton button[data-testid="stBaseButton-primary"]{
#             background:linear-gradient(135deg, var(--eb-primary), var(--eb-primary-dark));
#             border:none;
#             box-shadow:0 2px 10px rgba(79,70,229,0.28);
#         }
#         .stButton button[data-testid="stBaseButton-primary"]:hover{
#             box-shadow:0 4px 16px rgba(79,70,229,0.38);
#             transform:translateY(-1px);
#         }
#         .stDownloadButton > button{
#             border-radius:10px;
#             font-weight:600;
#         }

#         /* ---- Disabled buttons ----
#         Streamlit marks a disabled button with aria-disabled="true" (it does
#         NOT use the native disabled attribute), and its own default styling
#         only fades the label text — the background stays exactly as-is. This
#         overrides that so a disabled button is unmistakably non-clickable:
#         muted gray background, muted text, no shadow. */
#         .stButton button[aria-disabled="true"],
#         .stButton button:disabled{
#             background:#E3E5EC !important;
#             background-image:none !important;
#             color:#9AA1AE !important;
#             border:1px solid #E3E5EC !important;
#             box-shadow:none !important;
#             cursor:not-allowed !important;
#         }
#         .stButton button[aria-disabled="true"]:hover,
#         .stButton button:disabled:hover{
#             background:#E3E5EC !important;
#             box-shadow:none !important;
#             transform:none !important;
#         }

#         /* ---- Hover / focus border ----
#         Streamlit's default hover AND focus state colors the input border
#         with its theme red (applied via dynamically-swapped atomic classes,
#         not a plain :hover/:focus rule) — override it with the indigo
#         accent everywhere, on both hover and focus: text inputs, number
#         inputs (sidebar page range), selectboxes (sidebar document format),
#         and textareas. */
#         div[data-testid="stTextInputRootElement"]:hover,
#         div[data-testid="stTextInputRootElement"]:focus-within,
#         div[data-testid="stNumberInputContainer"]:hover,
#         div[data-testid="stNumberInputContainer"]:focus-within,
#         div[data-baseweb="input"]:hover,
#         div[data-baseweb="input"]:focus-within,
#         div[data-baseweb="textarea"]:hover,
#         div[data-baseweb="textarea"]:focus-within,
#         div[data-baseweb="select"]:hover,
#         div[data-baseweb="select"]:focus-within,
#         div[data-baseweb="select"] > div:hover,
#         div[data-baseweb="select"] > div:focus-within{
#             border-color: var(--eb-primary) !important;
#         }

#         /* Sidebar selectbox/number-input retain a focus ring in the same
#         red by default too -- neutralize it to match. */
#         div[data-baseweb="select"]:focus-within,
#         div[data-baseweb="base-input"]:focus-within{
#             box-shadow: 0 0 0 1px var(--eb-primary) !important;
#         }

#         /* ---- st.error() boxes ----
#         Streamlit's built-in st.error() uses its own native red theme
#         (background, left border, icon) independent of our custom classes.
#         There's no separate "danger" color in this palette, so it's
#         recolored to the same warning amber used elsewhere for caution
#         states. Covers both the current and a couple of older Streamlit
#         DOM patterns, since the exact markup has changed across versions. */
#         div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentError"]),
#         div[data-testid="stAlertContentError"],
#         div[data-testid="stAlert"][kind="error"],
#         div[data-baseweb="notification"][kind="negative"]{
#             background-color: var(--eb-warning-bg) !important;
#             border-color: var(--eb-warning-border) !important;
#             color: var(--eb-warning) !important;
#         }
#         div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentError"]) svg,
#         div[data-testid="stAlertContentError"] svg,
#         div[data-testid="stAlert"][kind="error"] svg,
#         div[data-baseweb="notification"][kind="negative"] svg{
#             fill: var(--eb-warning) !important;
#         }

#         /* ---- File uploader ---- */
#         div[data-testid="stFileUploaderDropzone"]{
#             border-radius:14px;
#             border:1.5px dashed var(--eb-border);
#             background:#FAFAFE;
#         }

#         /* ---- Expander ---- */
#         div[data-testid="stExpander"]{
#             border-radius:12px !important;
#             border:1px solid var(--eb-border) !important;
#         }

#         /* ---- Metrics ---- */
#         div[data-testid="stMetric"]{
#             background:#FAFAFD;
#             border:1px solid var(--eb-border);
#             border-radius:12px;
#             padding:0.7rem 0.9rem;
#         }

#         /* ---- Dataframe / data editor ---- */
#         div[data-testid="stDataFrame"], div[data-testid="stDataFrameResizable"]{
#             border-radius:12px;
#             overflow:hidden;
#             border:1px solid var(--eb-border);
#         }

#         /* ---- Progress bar ----
#         Streamlit renders st.progress() as:
#           div[data-testid="stProgress"]
#             > div (role="progressbar")          <- semantic wrapper only
#               > div[data-testid="stProgressBarTrack"]   <- the TRACK (full width, always visible)
#                 > div                                    <- the FILL (slides in via translateX)
#         The fill is the part that actually represents "completed", so it
#         needs the strong/branded color; the track is the "incomplete"
#         background and should stay clearly muted by comparison. */
#         div[data-testid="stProgressBarTrack"]{
#             background-color: var(--eb-border) !important;
#             border-radius:999px !important;
#             overflow:hidden;
#         }
#         div[data-testid="stProgressBarTrack"] > div{
#             background-color: var(--eb-primary) !important;
#             background-image: linear-gradient(90deg, var(--eb-primary), var(--eb-success)) !important;
#             border-radius:999px !important;
#         }

#         /* ---- Section captions ---- */
#         .eb-hint{
#             font-size:0.82rem;
#             color:var(--eb-text-muted);
#             margin-top:-0.3rem;
#             margin-bottom:0.4rem;
#         }
#         </style>
#         """,
#         unsafe_allow_html=True,
#     )


# def render_sample_pdf_viewer(pdf_path: str, height: int = 420) -> None:
#     """Render an inline preview of a sample PDF, or a friendly note if missing."""
#     path = Path(pdf_path)
#     if not path.exists():
#         st.caption(
#             f"No sample file found yet — add one at `{pdf_path}` to show a "
#             "preview here."
#         )
#         return

#     try:
#         pdf_bytes = path.read_bytes()
#     except OSError as exc:
#         st.caption(f"Could not read sample file `{pdf_path}` ({exc}).")
#         return

#     b64 = base64.b64encode(pdf_bytes).decode("utf-8")
#     st.markdown(
#         f'<iframe src="data:application/pdf;base64,{b64}" width="100%" '
#         f'height="{height}" style="border-radius:12px; border:1px solid '
#         f'var(--eb-border);"></iframe>',
#         unsafe_allow_html=True,
#     )
#     st.download_button(
#         "Download",
#         data=pdf_bytes,
#         file_name=path.name,
#         mime="application/pdf",
#         key=f"sample_dl_{path.name}",
#     )


# def render_sample_panel(label: str, document_type: str) -> None:
#     """Render the 'Sample <format>' card with a collapsible PDF preview."""
#     sample_path = SAMPLE_PDF_PATHS.get(document_type, "")
#     expander_key = f"sample_expanded__{document_type.replace(' ', '_')}"
#     default_expanded = expander_key not in st.session_state

#     with st.container(border=True):
#         st.markdown(f"##### {label}")
#         st.caption("A reference example of this layout, for orientation.")
#         with st.expander("Preview sample PDF", expanded=default_expanded, key=expander_key):
#             render_sample_pdf_viewer(sample_path)


# def collapse_sample_panel(document_type: str) -> None:
#     """Mark the sample panel for this format as collapsed on the next run."""
#     expander_key = f"sample_expanded__{document_type.replace(' ', '_')}"
#     st.session_state[expander_key] = False


# # ---------------------------------------------------------------------------
# # Output table column visibility (toolbar "Columns" dropdown)
# # ---------------------------------------------------------------------------

# AUDIT_COLUMNS = [
#     "source_page",
#     "segment_id",
#     "segment_name",
#     "needs_review",
#     "review_reason",
# ]

# DEFAULT_HIDDEN_COLUMNS = ["Description", "Details"]


# def _sync_show_all_checkbox() -> None:
#     """Keep the 'Show All' checkbox reflecting whether every column is visible."""
#     visible = st.session_state.get("col_visible", {})
#     if visible:
#         st.session_state["show_all_columns"] = all(visible.values())


# def _sync_hide_audit_checkbox() -> None:
#     """Keep the 'Hide audit columns' checkbox reflecting actual audit-column state."""
#     visible = st.session_state.get("col_visible", {})
#     present = [c for c in AUDIT_COLUMNS if c in visible]
#     if present:
#         st.session_state["hide_audit_columns"] = all(
#             not visible[c] for c in present
#         )


# def _on_column_visibility_toggle(col: str) -> None:
#     """Handle an individual column checkbox being (un)checked."""
#     st.session_state["col_visible"][col] = st.session_state[f"colvis__{col}"]
#     _sync_show_all_checkbox()
#     _sync_hide_audit_checkbox()


# def _on_show_all_toggle() -> None:
#     """Handle the 'Show All' checkbox: show everything, or restore prior state."""
#     visible = st.session_state.get("col_visible", {})
#     if st.session_state.get("show_all_columns"):
#         st.session_state["col_visible_snapshot"] = dict(visible)
#         for col in visible:
#             visible[col] = True
#             st.session_state[f"colvis__{col}"] = True
#     else:
#         snapshot = st.session_state.get("col_visible_snapshot")
#         if snapshot:
#             for col, was_visible in snapshot.items():
#                 if col in visible:
#                     visible[col] = was_visible
#                     st.session_state[f"colvis__{col}"] = was_visible
#     _sync_hide_audit_checkbox()


# def _on_hide_audit_toggle() -> None:
#     """
#     Handle the 'Hide audit columns' checkbox.

#     Checking it hides source_page/segment_id/segment_name/needs_review/
#     review_reason, remembering each one's prior visibility first.
#     Unchecking it restores each audit column to whatever it was set to
#     right before the checkbox was checked — so a column the user had
#     already hidden manually through the column list stays hidden.
#     """
#     visible = st.session_state.get("col_visible", {})
#     present = [c for c in AUDIT_COLUMNS if c in visible]

#     if st.session_state.get("hide_audit_columns"):
#         st.session_state["col_visible_audit_snapshot"] = {
#             c: visible[c] for c in present
#         }
#         for col in present:
#             visible[col] = False
#             st.session_state[f"colvis__{col}"] = False
#     else:
#         snapshot = st.session_state.get("col_visible_audit_snapshot") or {}
#         for col in present:
#             restored = snapshot.get(col, True)
#             visible[col] = restored
#             st.session_state[f"colvis__{col}"] = restored

#     _sync_show_all_checkbox()


# def inject_column_menu_patch() -> None:
#     """Strip 'Hide column' from the data editor's built-in ⋮ column menu.

#     Streamlit doesn't expose an API to selectively disable individual items
#     in that menu, so this patches the rendered DOM directly: it watches for
#     the menu being opened and hides any menu row whose label is exactly
#     "Hide column", leaving "Autosize" and "Pin column" untouched. Column
#     hide/show is handled instead by the "Columns" toolbar dropdown, whose
#     state (unlike the built-in menu's) is tracked in Python so it can
#     always be reversed.

#     This is best-effort DOM patching, not a public Streamlit API — if a
#     future Streamlit release changes the column menu's markup, this may
#     need updating to match.
#     """
#     st.markdown(
#         """
#         <script>
#         (function() {
#             function hideRow(el) {
#                 var row = el.closest('li') || el.parentElement;
#                 if (row) { row.style.display = "none"; }
#             }
#             function scan(root) {
#                 if (!root || !root.querySelectorAll) return;
#                 var nodes = root.querySelectorAll('*');
#                 for (var i = 0; i < nodes.length; i++) {
#                     var n = nodes[i];
#                     if (n.children.length === 0 &&
#                         n.textContent.trim() === "Hide column") {
#                         hideRow(n);
#                     }
#                 }
#             }
#             var observer = new MutationObserver(function(mutations) {
#                 mutations.forEach(function(m) {
#                     m.addedNodes.forEach(function(n) {
#                         if (n.nodeType === 1) { scan(n); }
#                     });
#                 });
#             });
#             observer.observe(document.body, { childList: true, subtree: true });
#         })();
#         </script>
#         """,
#         unsafe_allow_html=True,
#     )


# # ---------------------------------------------------------------------------
# # Step indicator
# # ---------------------------------------------------------------------------

# def build_workflow_steps(
#     document_type: str,
#     uploaded_file,
#     page_start: int,
#     page_end: int,
#     expected_columns: list[str],
#     key_column: str | None,
#     multiline_column: str | None,
#     extraction_done: bool,
# ) -> list[dict]:
#     steps = [
#         {"label": "Upload PDF", "done": uploaded_file is not None},
#         {"label": "Select page range", "done": (page_start != 1 or page_end != 1)},
#     ]

#     # "Other" allows multiline_column to be deliberately None (the user
#     # picked "extract as-is, no splitting"), so it isn't required for
#     # the step to be considered done there. Formatted 1 and Formatted 2
#     # have no such option and always need a real column selected.
#     if document_type == "Other":
#         columns_ready = bool(expected_columns) and bool(key_column)
#     else:
#         columns_ready = (
#             bool(expected_columns) and bool(key_column) and bool(multiline_column)
#         )
#     steps.append({"label": "Configure columns", "done": columns_ready})
#     steps.append({"label": "Run extraction", "done": extraction_done})

#     # A step can only be considered complete if every step before it is
#     # also complete. Without this, each step's "done" flag is independent,
#     # so e.g. removing the uploaded PDF (which flips "Upload PDF" back to
#     # not-done) had no effect on "Run extraction", which stayed "done"
#     # forever just because a result from an earlier run was still sitting
#     # in session state. This makes completion cascade: the first not-done
#     # step, and everything after it, always reads as incomplete.
#     all_done_so_far = True
#     for step in steps:
#         if not all_done_so_far:
#             step["done"] = False
#         all_done_so_far = all_done_so_far and step["done"]

#     return steps


# def render_stepper(slot, steps: list[dict]) -> None:
#     first_pending_seen = False
#     html_parts = ['<div class="stepper-wrap">']

#     for i, step in enumerate(steps):
#         if step["done"]:
#             state = "done"
#             icon = "✓"
#         elif not first_pending_seen:
#             state = "active"
#             icon = str(i + 1)
#             first_pending_seen = True
#         else:
#             state = "pending"
#             icon = str(i + 1)

#         html_parts.append(
#             f'<div class="step {state}">'
#             f'<div class="step-line"></div>'
#             f'<div class="step-circle">{icon}</div>'
#             f'<div class="step-label">{step["label"]}</div>'
#             f"</div>"
#         )

#     html_parts.append("</div>")

#     with slot.container():
#         st.markdown("".join(html_parts), unsafe_allow_html=True)


# # ---------------------------------------------------------------------------
# # Extraction with live progress (UI-only: the backend call itself is
# # untouched, it just now runs on a worker thread so the UI can keep
# # updating a progress bar / status text while it works).
# # ---------------------------------------------------------------------------

# def estimate_extraction_seconds(document_type: str, page_start: int, page_end: int) -> int:
#     page_count = max(1, int(page_end) - int(page_start) + 1)
#     base = 6 + (page_count * 3)
#     if document_type == "Other":
#         base = int(base * 1.6)
#     return max(8, base)


# def run_extraction_with_progress(extract_kwargs: dict, estimated_seconds: int):
#     """Run extract_tables_from_pdf on a background thread while updating a
#     progress bar / status text / elapsed timer in the main thread."""

#     result_container = {"df": None, "error": None, "done": False}

#     def worker():
#         try:
#             result_container["df"] = extract_tables_from_pdf(**extract_kwargs)
#         except Exception as exc:  # noqa: BLE001
#             result_container["error"] = exc
#         finally:
#             result_container["done"] = True

#     thread = threading.Thread(target=worker, daemon=True)
#     thread.start()

#     status_placeholder = st.empty()
#     progress_bar = st.progress(0)
#     detail_placeholder = st.empty()

#     start_time = time.time()
#     n_steps = len(STATUS_STEPS)

#     while not result_container["done"]:
#         elapsed = time.time() - start_time
#         fraction = min(elapsed / estimated_seconds, 0.97)
#         step_idx = min(int(fraction * n_steps), n_steps - 1)

#         status_placeholder.markdown(f"**{STATUS_STEPS[step_idx]}…**")
#         progress_bar.progress(fraction)

#         remaining = max(0, estimated_seconds - elapsed)
#         detail_placeholder.caption(
#             f"Elapsed {int(elapsed)}s · est. {int(remaining)}s remaining · "
#             f"{int(fraction * 100)}% complete"
#         )

#         time.sleep(0.25)

#     total_elapsed = time.time() - start_time
#     progress_bar.progress(1.0)
#     status_placeholder.markdown("**Finished**")
#     detail_placeholder.caption(f"Completed in {total_elapsed:.1f}s")

#     result_container["elapsed_seconds"] = total_elapsed

#     return result_container


# # ---------------------------------------------------------------------------
# # Page setup
# # ---------------------------------------------------------------------------

# st.set_page_config(
#     page_title="EDI Extractor",
#     layout="wide",
# )

# inject_global_css()
# inject_column_menu_patch()

# # ---------------------------------------------------------------------------
# # Sidebar — all inputs live here, top to bottom in workflow order
# # ---------------------------------------------------------------------------

# with st.sidebar:
#     st.markdown("##### 1 · Upload")
#     uploaded_file = st.file_uploader("Upload PDF", type=["pdf"], label_visibility="collapsed")

#     st.markdown("##### Document format")
#     document_type = st.selectbox(
#         "Select document format",
#         options=["Formatted 1", "Formatted 2", "Other"],
#         index=0,
#         label_visibility="collapsed",
#         help=(
#             "Formatted 1 is the structured EDI guide format with Segment ID, "
#             "metadata box, and Element Summary table. Other uses the generic "
#             "AI extractor."
#         ),
#     )

#     st.markdown("##### 2 · Page range")
#     st.caption(PAGE_RANGE_HINT)

#     page_col1, page_col2 = st.columns(2)
#     with page_col1:
#         page_start = st.number_input("Start page", min_value=1, value=1, step=1)
#     with page_col2:
#         page_end = st.number_input("End page", min_value=1, value=1, step=1)

#     # "Other" mode AI extraction settings (DPI / Azure model) are fixed
#     # internally — see OTHER_MODE_DPI / OTHER_MODE_MODEL above — and are no
#     # longer exposed in the UI.
#     dpi = OTHER_MODE_DPI
#     model = OTHER_MODE_MODEL


# # ---------------------------------------------------------------------------
# # Main area — header + stepper (stepper content filled in further down,
# # once all the format-specific settings below are known for this run)
# # ---------------------------------------------------------------------------

# st.markdown('<div class="eb-eyebrow">EDI 850 · PURCHASE ORDER PARSER</div>', unsafe_allow_html=True)
# st.markdown('<div class="eb-title">EDI Extractor</div>', unsafe_allow_html=True)
# # st.markdown(
# #     '<div class="eb-subtitle">Turn EDI 850 purchase order PDFs into a clean, '
# #     "reviewable table — upload, choose your page range, and run the "
# #     "extraction.</div>",
# #     unsafe_allow_html=True,
# # )
# st.markdown(f'<span class="eb-chip">{document_type}</span>', unsafe_allow_html=True)

# stepper_slot = st.empty()


# # ---------------------------------------------------------------------------
# # Format-specific settings
# #
# # Note: the sample preview panel is rendered BEFORE the settings card for
# # Formatted 2 and Other, per the requested section order.
# # ---------------------------------------------------------------------------

# if document_type == "Formatted 1":
#     include_audit_columns = True

#     render_sample_panel("The Sample Format 1", document_type)

#     with st.container(border=True):
#         st.markdown("##### Extraction Settings")

#         st.markdown("###### Table columns from the PDF")
#         columns_text = st.text_area(
#             "Write one column per line",
#             value=DEFAULT_FORMATTED1_COLUMNS,
#             height=160,
#             key="f1_columns_text",
#         )
#         expected_columns = split_column_text(columns_text)

#         if expected_columns:
#             key_column = st.selectbox(
#                 "Key column: a new record starts when this column has a value",
#                 options=expected_columns,
#                 index=0,
#                 key="f1_key_column",
#                 help=(
#                     "The column holding values like BEG01, BEG02, BEG03. Used "
#                     "to detect where each element record begins, including "
#                     "when a segment's table continues onto the next page."
#                 ),
#             )

#             guessed_multiline_index = 0
#             for idx, col in enumerate(expected_columns):
#                 if "name" in col.lower():
#                     guessed_multiline_index = idx
#                     break

#             multiline_column = st.selectbox(
#                 "Multi-line column containing element name/qualifiers",
#                 options=expected_columns,
#                 index=guessed_multiline_index,
#                 key="f1_multiline_column",
#                 help=(
#                     "Usually Element Name. This is the column whose first "
#                     "line is the element name, optionally followed by a "
#                     "description and/or a qualifier code/name list — all of "
#                     "which are split out into the Description, Qualifiers, "
#                     "and Details output columns automatically."
#                 ),
#             )
#         else:
#             key_column = ""
#             multiline_column = None

#         extra_user_instructions = ""

# elif document_type == "Formatted 2":
#     include_audit_columns = True

#     render_sample_panel("The Sample Format 2", document_type)

#     with st.container(border=True):
#         st.markdown("##### Extraction Settings")
#         # st.info(
#         #     "Formatted 2 reads each segment section that starts with 'Segment:' "
#         #     "(e.g. BEG, FOB, REF), pulling out the segment id/name, Loop, and Level "
#         #     "from the metadata box above the table. For the element table itself, "
#         #     "it locates each row by its Ref/Element-Number pattern (not by exact "
#         #     "column text), so it copes with multi-line element names, qualifier "
#         #     "code/name mini-tables (turned into 'CODE = Name' rows, with extra "
#         #     "qualifier-specific notes added as '[...]'), and repeating page "
#         #     "headers/footers, which are stripped automatically."
#         # )

#         st.markdown("###### Table columns from the PDF")
#         columns_text = st.text_area(
#             "Write one column per line",
#             value=DEFAULT_FORMATTED2_COLUMNS,
#             height=160,
#         )
#         expected_columns = split_column_text(columns_text)

#         if expected_columns:
#             key_column = st.selectbox(
#                 "Key column: a new record starts when this column has a value",
#                 options=expected_columns,
#                 index=0,
#                 help=(
#                     "The column holding values like FOB01, FOB02, FOB03. Used "
#                     "together with the multi-line column below to anchor where "
#                     "the table begins."
#                 ),
#             )

#             guessed_multiline_index = 0
#             for idx, col in enumerate(expected_columns):
#                 if "name" in col.lower():
#                     guessed_multiline_index = idx
#                     break

#             multiline_column = st.selectbox(
#                 "Multi-line column containing element name/qualifiers",
#                 options=expected_columns,
#                 index=guessed_multiline_index,
#                 help=(
#                     "Usually Element Name, Name, or NAME. This is the column "
#                     "whose first line is the element name, optionally followed "
#                     "by a description and/or a qualifier code/name list — all "
#                     "of which are split out into the Description and "
#                     "Qualifiers output columns automatically."
#                 ),
#             )
#         else:
#             key_column = ""
#             multiline_column = None

#         extra_user_instructions = ""

# else:  # "Other"
#     include_audit_columns = True

#     with st.container(border=True):
#         st.markdown("##### Extraction Settings")
#         # st.info(
#         #     "Other uses AI vision extraction for PDFs that don't follow the "
#         #     "Formatted 1/2 layouts. Pick the columns visible in the table, "
#         #     "then choose a multi-line column if one of them packs the "
#         #     "element name and a qualifier code/name list together — "
#         #     "implicit (spacing-separated) or explicit (a 'Code'/'Name' "
#         #     "mini-table) qualifier lists are both detected automatically "
#         #     "and turned into 'CODE = Name' entries, with any "
#         #     "qualifier-specific notes added as '[...]'. Any description or "
#         #     "comment text in that column is ignored."
#         # )

#         st.markdown("###### Table columns from the PDF")
#         columns_text = st.text_area(
#             "Write one column per line",
#             value=DEFAULT_OTHER_COLUMNS,
#             height=160,
#         )
#         expected_columns = split_column_text(columns_text)

#         if expected_columns:
#             key_column = st.selectbox(
#                 "Key column: a new record starts when this column has a value",
#                 options=expected_columns,
#                 index=0,
#                 help=(
#                     "The column that identifies each row, e.g. Ref, "
#                     "Reference Designator, or ID. Used together with the "
#                     "multi-line column below to anchor where each record "
#                     "begins."
#                 ),
#             )

#             no_split_option = "None — extract every column as-is, no splitting"
#             multiline_options = [no_split_option] + expected_columns

#             guessed_multiline_index = 0
#             for idx, col in enumerate(expected_columns):
#                 if "name" in col.lower():
#                     guessed_multiline_index = idx + 1
#                     break

#             multiline_selection = st.selectbox(
#                 "Multi-line column containing element name/qualifiers",
#                 options=multiline_options,
#                 index=guessed_multiline_index,
#                 help=(
#                     "Usually Element Name, Name, or NAME. The element name "
#                     "and any qualifier code/name list found in this column "
#                     "are split out into the Element Name and Qualifiers "
#                     "output columns automatically. Any description/comment "
#                     "text in this column is ignored, not extracted. Pick "
#                     "'None' to skip this entirely and just extract every "
#                     "column's visible text verbatim, multi-line cells kept "
#                     "as-is with their original line breaks."
#                 ),
#             )

#             multiline_column = (
#                 None if multiline_selection == no_split_option else multiline_selection
#             )
#         else:
#             key_column = ""
#             multiline_column = None

#         extra_user_instructions = st.text_area(
#             "Extra instructions for AI (optional)",
#             value=DEFAULT_EXTRA_INSTRUCTIONS,
#             height=100,
#         )


# # ---------------------------------------------------------------------------
# # Stepper render (now that all format-specific settings are known)
# # ---------------------------------------------------------------------------

# current_file_signature = (
#     (uploaded_file.name, uploaded_file.size) if uploaded_file is not None else None
# )
# extraction_done = (
#     current_file_signature is not None
#     and "result_df" in st.session_state
#     and st.session_state.get("result_df_source") == current_file_signature
# )

# workflow_steps = build_workflow_steps(
#     document_type=document_type,
#     uploaded_file=uploaded_file,
#     page_start=page_start,
#     page_end=page_end,
#     expected_columns=expected_columns,
#     key_column=key_column,
#     multiline_column=multiline_column,
#     extraction_done=extraction_done,
# )
# render_stepper(stepper_slot, workflow_steps)


# # ---------------------------------------------------------------------------
# # Validation
# # ---------------------------------------------------------------------------

# validation_errors = []

# if uploaded_file is None:
#     validation_errors.append("Please upload a PDF file.")

# if page_end < page_start:
#     validation_errors.append("End page must be greater than or equal to start page.")

# if document_type in {"Formatted 1", "Formatted 2", "Other"}:
#     if not expected_columns:
#         validation_errors.append("Please enter at least one source column.")
#     if key_column and key_column not in expected_columns:
#         validation_errors.append("Key column must be one of the source columns.")
#     if multiline_column and multiline_column not in expected_columns:
#         validation_errors.append("Multi-line column must be one of the source columns.")

# for error in validation_errors:
#     st.warning(error)


# # ---------------------------------------------------------------------------
# # Run extraction
# # ---------------------------------------------------------------------------

# with st.container(border=True):
#     run_col, info_col = st.columns([1, 2])
#     with run_col:
#         run_button = st.button(
#             "Run extraction",
#             type="primary",
#             disabled=bool(validation_errors),
#             width="stretch",
#         )
#     with info_col:
#         est_seconds = estimate_extraction_seconds(document_type, page_start, page_end)
#         # st.markdown(
#         #     f'<div class="eb-hint">Estimated time for this run: ~{est_seconds}s '
#         #     "(varies with page count and format).</div>",
#         #     unsafe_allow_html=True,
#         # )

# if run_button and uploaded_file is not None:
#     with tempfile.TemporaryDirectory() as tmp_dir:
#         pdf_path = Path(tmp_dir) / uploaded_file.name
#         with open(pdf_path, "wb") as f:
#             f.write(uploaded_file.getbuffer())

#         extract_kwargs = {
#             "pdf_path": str(pdf_path),
#             "page_start": int(page_start),
#             "page_end": int(page_end),
#             "expected_columns": expected_columns,
#             "key_column": key_column,
#             "extra_user_instructions": extra_user_instructions,
#             "model": model if document_type == "Other" else None,
#             "dpi": int(dpi) if document_type == "Other" else 220,
#             "document_type": document_type,
#             "include_audit_columns": include_audit_columns,
#         }

#         # All three document types now use expected_columns/key_column; all
#         # three also use multiline_column (Formatted 1 splits it into
#         # Element Name/Description/Qualifiers/Details, same idea as the
#         # other two routes).
#         extract_kwargs["multiline_column"] = multiline_column

#         est_seconds = estimate_extraction_seconds(document_type, page_start, page_end)

#         with st.status("Running extraction…", expanded=True) as status_box:
#             result = run_extraction_with_progress(extract_kwargs, est_seconds)

#             if result["error"] is not None:
#                 status_box.update(label="Extraction failed", state="error", expanded=True)
#                 st.error(f"Extraction failed: {result['error']}")
#                 st.stop()

#             status_box.update(label="Extraction finished", state="complete", expanded=False)

#         st.session_state["result_df"] = result["df"]
#         st.session_state["result_df_source"] = (uploaded_file.name, uploaded_file.size)
#         st.session_state["result_run_info"] = {
#             "pdf_filename": uploaded_file.name,
#             "extraction_mode": document_type,
#             "page_start": int(page_start),
#             "page_end": int(page_end),
#             "processing_time_seconds": result.get("elapsed_seconds"),
#         }
#         collapse_sample_panel(document_type)
#         st.rerun()


# # ---------------------------------------------------------------------------
# # Results
# # ---------------------------------------------------------------------------

# if "result_df" in st.session_state:
#     result_df = st.session_state["result_df"]

#     if result_df is None:
#         st.error("Backend returned None instead of a DataFrame.")
#         st.stop()

#     if not isinstance(result_df, pd.DataFrame):
#         st.error(f"Backend returned {type(result_df)}, expected pandas DataFrame.")
#         st.stop()

#     st.markdown("#### Extraction result")

#     if result_df.empty:
#         st.warning(
#             "No rows were extracted. Try changing page range, source columns, "
#             "key column, DPI, or prompt instructions."
#         )
#     else:
#         total_rows = len(result_df)

#         if "needs_review" in result_df.columns:
#             review_count = int(result_df["needs_review"].fillna(False).astype(bool).sum())
#         else:
#             review_count = 0

#         with st.container(border=True):
#             col1, col2, col3 = st.columns(3)
#             col1.metric("Rows extracted", total_rows)
#             col2.metric("Rows needing review", review_count)
#             if total_rows:
#                 col3.metric("Review ratio", f"{review_count / total_rows:.0%}")
#             else:
#                 col3.metric("Review ratio", "0%")

#         # -----------------------------------------
#         # Extraction Report
#         # -----------------------------------------
#         run_info = st.session_state.get("result_run_info", {}) or {}

#         extraction_report = build_extraction_report(
#             df=result_df,
#             pdf_filename=run_info.get("pdf_filename"),
#             extraction_mode=run_info.get("extraction_mode"),
#             page_start=run_info.get("page_start"),
#             page_end=run_info.get("page_end"),
#             processing_time_seconds=run_info.get("processing_time_seconds"),
#         )

#         render_extraction_report(extraction_report)

#         # -----------------------------------------
#         # Rows needing review section
#         # -----------------------------------------
#         if "needs_review" in result_df.columns:
#             review_mask = result_df["needs_review"].fillna(False).astype(bool)
#         else:
#             review_mask = pd.Series([False] * len(result_df))

#         review_df = result_df[review_mask].copy()

#         if not review_df.empty:
#             with st.container(border=True):
#                 st.markdown("##### Rows needing review")
#                 st.warning(
#                     "Please review these rows before "
#                     "downloading the final file."
#                 )
#                 st.dataframe(
#                     review_df.style.apply(highlight_review_rows, axis=1),
#                     width="stretch",
#                     height=250,
#                 )

#         # -----------------------------------------
#         # Editable final table
#         # -----------------------------------------
#         with st.container(border=True):
#             st.markdown("##### Editable extraction table")

#             final_output_df = result_df
#             all_columns = list(final_output_df.columns)

#             # Reset column-visibility state whenever the column set changes
#             # (e.g. a new extraction run against a different vendor/format).
#             if st.session_state.get("col_visible_columns_source") != all_columns:
#                 st.session_state["col_visible_columns_source"] = all_columns
#                 st.session_state["col_visible"] = {
#                     c: c not in DEFAULT_HIDDEN_COLUMNS for c in all_columns
#                 }
#                 st.session_state["col_visible_snapshot"] = None
#                 st.session_state["col_visible_audit_snapshot"] = None
#                 st.session_state["show_all_columns"] = not any(
#                     c in all_columns for c in DEFAULT_HIDDEN_COLUMNS
#                 )
#                 st.session_state["hide_audit_columns"] = False
#                 st.session_state["col_search_query"] = ""
#                 for c in all_columns:
#                     st.session_state[f"colvis__{c}"] = c not in DEFAULT_HIDDEN_COLUMNS

#             with st.container(key="output_table_toolbar"):
#                 with st.popover(":material/visibility: Columns"):
#                     st.markdown(
#                         '<div class="eb-colpanel-header">'
#                         '<div class="eb-colpanel-title">Show / Hide Columns</div>'
#                         "</div>",
#                         unsafe_allow_html=True,
#                     )

#                     search_query = st.text_input(
#                         "Search columns",
#                         key="col_search_query",
#                         placeholder="Search in columns...",
#                         label_visibility="collapsed",
#                     )

#                     st.checkbox(
#                         "Show All",
#                         key="show_all_columns",
#                         on_change=_on_show_all_toggle,
#                     )

#                     st.checkbox(
#                         "Hide audit columns",
#                         key="hide_audit_columns",
#                         on_change=_on_hide_audit_toggle,
#                     )

#                     # st.divider()

#                     query = search_query.strip().lower()
#                     matching_columns = (
#                         [c for c in all_columns if query in c.lower()]
#                         if query
#                         else all_columns
#                     )

#                     if not matching_columns:
#                         st.caption("No columns match your search.")
#                     else:
#                         # Header/search/Show All stay put; only this inner
#                         # fixed-height container scrolls when there are many
#                         # columns.
#                         with st.container(height=220):
#                             for col in matching_columns:
#                                 st.checkbox(
#                                     col,
#                                     key=f"colvis__{col}",
#                                     on_change=_on_column_visibility_toggle,
#                                     args=(col,),
#                                 )

#                 download_slot = st.empty()

#             visible_columns = [
#                 c for c in all_columns if st.session_state["col_visible"].get(c, True)
#             ]
#             if not visible_columns:
#                 st.warning("Select at least one column to display and export.")
#                 visible_columns = all_columns

#             edited_df = st.data_editor(
#                 final_output_df,
#                 column_order=visible_columns,
#                 width="stretch",
#                 height=500,
#                 num_rows="dynamic",
#                 hide_index=False,
#                 key="final_editable_table",
#             )

#             export_df = edited_df[visible_columns]
#             excel_bytes = dataframe_to_excel_bytes_with_report(export_df, extraction_report)

#             download_slot.download_button(
#                 ":material/download: Download Excel",
#                 data=excel_bytes,
#                 file_name="edi_ai_extraction_result.xlsx",
#                 mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
#                 width="stretch",
#             )


































"""
EDI 850 PDF Extractor — Streamlit UI

This file only contains UI/UX/presentation code. The extraction and export
logic still lives in `backend/extractor.py` and `backend/export_utils.py` and
is called exactly the same way it always was — nothing about how PDFs are
parsed, mapped, or exported has changed.
"""

import base64
import re
import threading
import time
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from backend.export_utils import dataframe_to_excel_bytes_with_report
from backend.extractor import extract_tables_from_pdf
from backend.report_utils import build_extraction_report


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Update these paths to point at the sample PDFs you ship with the app.
# They are only used to render the inline "Sample" preview panels — nothing
# else depends on them, and a missing file just hides the preview.
SAMPLE_PDF_PATHS = {
    "Formatted 1": "assets/samples/formatted_1_sample.pdf",
    "Formatted 2": "assets/samples/formatted_2_sample.pdf",
    "Other": "assets/samples/other_sample.pdf",
}

# Status messages cycled through while extraction runs in the background.
STATUS_STEPS = [
    "Preparing uploaded PDF",
    "Reading selected pages",
    "Detecting format/layout",
    "Extracting segment tables",
    "Splitting merged columns",
    "Cleaning element numbers and names",
    "Detecting Loop and Level",
    "Generating final table",
]

# Default columns for Formatted 1's "Table columns from the PDF" text area.
DEFAULT_FORMATTED1_COLUMNS = """Ref
Id
Element Name
Req
Type
Min/Max
Usage"""

# Default columns for Formatted 2's "Table columns from the PDF" text area.
DEFAULT_FORMATTED2_COLUMNS = """Ref. Des.
Data Element
Name
Attributes"""

DEFAULT_OTHER_COLUMNS = """Ref
Id
Element Name
Req
Type
Min/Max
Usage"""

DEFAULT_EXTRA_INSTRUCTIONS = (
    "Extract only the element summary table. Ignore page headers, footers, "
    "segment titles, and unrelated text."
)

# Fixed AI-extraction defaults for "Other" mode. There is no UI for these —
# see the "Improve Other-mode UI" change. Edit the values here if you ever
# need to change them.
OTHER_MODE_DPI = 220
OTHER_MODE_MODEL = "gpt-4.1-mini"

# Path to the company logo shown in the fixed top header. Point this at your
# logo asset (e.g. an image dropped into an "assets/branding/" folder next
# to this file). If the file isn't found, the header falls back to a plain
# text placeholder so the app still renders cleanly — just update this path
# (or drop your file at this exact path) once you have a logo to use.
COMPANY_LOGO_PATH = "assets/branding/company_logo.png"


# ---------------------------------------------------------------------------
# Small data-shaping helpers (unchanged logic, just kept local to the UI)
# ---------------------------------------------------------------------------

def split_column_text(text: str) -> list[str]:
    """Convert multiline input into list of columns."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def highlight_review_rows(row):
    """Highlight rows that need review using the brand accent red (soft tint)."""
    needs_review = bool(row.get("needs_review", False))
    if needs_review:
        return ["background-color: rgba(166,33,38,0.10)"] * len(row)
    return [""] * len(row)


# ---------------------------------------------------------------------------
# "Qualifiers / Code Values" post-processing
#
# The backend's extraction routes (Formatted 1/2, Other) are untouched — this
# is a pure post-processing + display layer bolted on afterward. It renames
# the backend's original "Qualifiers" output column to "Qualifiers / Code
# Values" (treated as the merged source of truth), classifies each row into
# one of three split-out columns ("Qualifiers", "Code Values", "Data"), and
# lets the "Editable extraction table" toolbar pick which of the merged vs.
# split columns are actually present for viewing/editing/export.
# ---------------------------------------------------------------------------

QUALIFIERS_MERGED_COLUMN = "Qualifiers / Code Values"
QUALIFIERS_SPLIT_COLUMNS = ["Qualifiers", "Code Values", "Data"]

QUALIFIER_DISPLAY_MODES = ["Merged only", "Split only", "Both"]
DEFAULT_QUALIFIER_DISPLAY_MODE = "Merged only"

# Element Name substrings that mean "this is a coded element" (rule C).
_CODE_ELEMENT_NAME_KEYWORDS = [
    "code",
    "type",
    "method",
    "status",
    "purpose",
    "identifier code",
]

# Element Name substrings that mean "this is actual business/sample data"
# (rule E) — only checked once rules B/C/D have all failed to match.
_DATA_ELEMENT_NAME_KEYWORDS = [
    "number",
    "date",
    "quantity",
    "price",
    "amount",
    "name",
    "address",
    "description",
    "message",
    "text",
]

# Rule D: recognizes both "CODE = Meaning" and "CODE Meaning" style code-list
# lines (e.g. "00 = Original", "07 = Duplicate", "BY Buying Party",
# "002 Delivery Requested"). Only the first non-blank line is checked, since
# a cell holding a whole code list is still a code list even if later lines
# don't individually look like one. The code token itself is required to be
# purely digits or purely UPPERCASE letters (1-4 chars) — not just any short
# word — otherwise ordinary two-word data like "Acme Corp" or "New York"
# would also look like a code list.
_CODE_LIST_EQUALS_PATTERN = re.compile(r"^(?:[0-9]{1,4}|[A-Z]{1,4})\s*=\s*\S")
_CODE_LIST_SPACE_PATTERN = re.compile(r"^(?:[0-9]{1,4}|[A-Z]{1,4})\s+[A-Za-z].*")


def _looks_like_code_list(value: str) -> bool:
    """Rule D: does the cell's content itself look like a code-list line?"""
    first_line = next((ln.strip() for ln in value.splitlines() if ln.strip()), "")
    if not first_line:
        return False
    return bool(
        _CODE_LIST_EQUALS_PATTERN.match(first_line)
        or _CODE_LIST_SPACE_PATTERN.match(first_line)
    )


def _classify_qualifier_cell(value: str, element_name: str) -> str:
    """Classify one row into "Qualifiers", "Code Values", or "Data".

    Implements rules B–F from the spec, in order, first match wins. Rule A
    (empty cell) is handled by the caller before this is reached.
    """
    element_name_lower = element_name.lower()

    # B: Element Name says this is a qualifier.
    if "qualifier" in element_name_lower:
        return "Qualifiers"

    # C: Element Name suggests a coded element.
    if any(kw in element_name_lower for kw in _CODE_ELEMENT_NAME_KEYWORDS):
        return "Code Values"

    # D: the content itself looks like a code list.
    if _looks_like_code_list(value):
        return "Code Values"

    # E: Element Name suggests actual business/sample data.
    if any(kw in element_name_lower for kw in _DATA_ELEMENT_NAME_KEYWORDS):
        return "Data"

    # F: uncertain -> safest fallback is Code Values.
    return "Code Values"


def split_qualifiers_code_values_column(df: pd.DataFrame) -> pd.DataFrame:
    """Rename "Qualifiers" -> "Qualifiers / Code Values" and add the split
    "Qualifiers" / "Code Values" / "Data" columns derived from it.

    Leaves every other column untouched, and drops the new "Data" column
    again if it ends up entirely empty (rule: don't add a column with
    nothing in it — see the spec's "Drop the Data column" requirement).
    """
    df = df.copy()

    if "Qualifiers" in df.columns and QUALIFIERS_MERGED_COLUMN not in df.columns:
        df = df.rename(columns={"Qualifiers": QUALIFIERS_MERGED_COLUMN})

    if QUALIFIERS_MERGED_COLUMN not in df.columns:
        # Nothing to split — leave the DataFrame exactly as it came in.
        return df

    element_name_col = "Element Name" if "Element Name" in df.columns else None

    qualifiers_col: list[str] = []
    code_values_col: list[str] = []
    data_col: list[str] = []

    for _, row in df.iterrows():
        raw_value = row.get(QUALIFIERS_MERGED_COLUMN, "")
        value = "" if pd.isna(raw_value) else str(raw_value)

        element_name = ""
        if element_name_col is not None:
            raw_element_name = row.get(element_name_col, "")
            element_name = "" if pd.isna(raw_element_name) else str(raw_element_name)

        if not value.strip():
            # A: empty source cell -> every split column stays empty.
            qualifiers_col.append("")
            code_values_col.append("")
            data_col.append("")
            continue

        bucket = _classify_qualifier_cell(value, element_name)
        qualifiers_col.append(value if bucket == "Qualifiers" else "")
        code_values_col.append(value if bucket == "Code Values" else "")
        data_col.append(value if bucket == "Data" else "")

    df["Qualifiers"] = qualifiers_col
    df["Code Values"] = code_values_col
    df["Data"] = data_col

    if df["Data"].astype(str).str.strip().eq("").all():
        df = df.drop(columns=["Data"])

    # pandas appends new columns at the far right by default, which is why
    # "Qualifiers"/"Code Values"/"Data" used to show up at the end of the
    # table instead of near the merged column they came from. Move them
    # back to sit immediately after QUALIFIERS_MERGED_COLUMN's original
    # spot instead. apply_qualifier_display_mode() below only ever drops
    # columns from this order (never reorders), so "Split only" naturally
    # ends up with the split columns exactly where the merged column used
    # to be, and "Both" naturally ends up with merged-then-split, adjacent.
    split_columns_present = [c for c in QUALIFIERS_SPLIT_COLUMNS if c in df.columns]
    other_columns = [c for c in df.columns if c not in split_columns_present]
    merged_col_position = other_columns.index(QUALIFIERS_MERGED_COLUMN)
    ordered_columns = (
        other_columns[: merged_col_position + 1]
        + split_columns_present
        + other_columns[merged_col_position + 1 :]
    )
    df = df[ordered_columns]

    return df


def apply_qualifier_display_mode(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    """Drop whichever qualifier-related columns don't belong in `mode`.

    "Merged only" drops the split columns; "Split only" drops the merged
    column; "Both" drops nothing. Every non-qualifier column is untouched,
    and this is a no-op for columns that were already absent (e.g. "Data"
    when it was empty and dropped by split_qualifiers_code_values_column).
    """
    present_split_columns = [c for c in QUALIFIERS_SPLIT_COLUMNS if c in df.columns]

    if mode == "Split only":
        drop_columns = (
            [QUALIFIERS_MERGED_COLUMN] if QUALIFIERS_MERGED_COLUMN in df.columns else []
        )
    elif mode == "Both":
        drop_columns = []
    else:  # "Merged only" (and the default/fallback case)
        drop_columns = present_split_columns

    return df.drop(columns=drop_columns) if drop_columns else df


def render_extraction_report(report: dict) -> None:
    """Render the Extraction Report as a collapsible section."""

    with st.expander("Extraction Report", expanded=False):
        st.markdown("###### File Information")
        st.table(
            pd.DataFrame(
                list(report["file_info"].items()), columns=["Field", "Value"]
            ).set_index("Field")
        )

        st.markdown("###### Processing Summary")
        st.table(
            pd.DataFrame(
                list(report["processing_summary"].items()),
                columns=["Metric", "Value"],
            ).set_index("Metric")
        )

        st.markdown("###### Segment Summary")
        segment_summary = report["segment_summary"]
        if segment_summary.empty:
            st.caption("No segment information available for this extraction mode.")
        else:
            st.dataframe(segment_summary, width="stretch", hide_index=True)

        st.markdown("###### Quality Summary")
        st.table(
            pd.DataFrame(
                list(report["quality_summary"].items()),
                columns=["Metric", "Value"],
            ).set_index("Metric")
        )

        st.markdown("###### Extraction Confidence / Status")
        status_info = report["status"]
        status_style = {
            "Excellent": st.success,
            "Good": st.success,
            "Moderate": st.warning,
            "Needs Careful Review": st.error,
            "No Data Extracted": st.error,
        }.get(status_info["status"], st.info)
        status_style(f"**Status: {status_info['status']}**\n\n{status_info['explanation']}")

        st.markdown("###### Warnings and Recommendations")
        for warning_text in report["warnings"]:
            st.markdown(f"- {warning_text}")

        review_reason_summary = report["review_reason_summary"]
        if not review_reason_summary.empty:
            st.markdown("###### Most Common Review Reasons")
            st.dataframe(review_reason_summary, width="stretch", hide_index=True)


# ---------------------------------------------------------------------------
# Visual styling
# ---------------------------------------------------------------------------

def inject_global_css() -> None:
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@500;600&display=swap');

        :root{
            --eb-bg:#FFFFFF;
            --eb-sidebar-bg:#F7F7F7;
            --eb-card:#FFFFFF;
            --eb-border:#ACB0B0;
            --eb-border-strong:rgba(17,20,38,0.28);
            --eb-text:#111426;
            --eb-text-muted:rgba(17,20,38,0.6);
            --eb-primary:#A62126;
            --eb-primary-soft:rgba(166,33,38,0.08);
            --eb-primary-border:rgba(166,33,38,0.32);
            --eb-navy:#1A1E40;
            --eb-muted:#ACB0B0;
            --eb-header-height:4.75rem;
        }

        html, body{
            margin: 0 !important;
            padding: 0 !important;
            background-color: var(--eb-navy) !important;
        }

        html, body, [class*="css"]  { font-family: 'Inter', sans-serif; }

        .stApp { background: var(--eb-bg); }

        /* ---- Fixed header (company logo) ----
        Streamlit's own header (data-testid="stHeader") normally computes
        its own width based on the sidebar's default width, so once the
        sidebar is widened it stops spanning the sidebar's column — that's
        why the navy background previously only showed up over the main
        content area. left/right/width are forced here (with !important,
        which beats even an inline style) so the header is unambiguously
        full-width, above both the sidebar and the main content, and is
        the first visible block on the page.

        Streamlit's built-in toolbar/decoration elements sit in that same
        top strip with their own default (black, unthemed) backgrounds
        independent of our header color — those are neutralized to
        transparent here so nothing but our navy header shows in that
        band. The logo itself is inserted as an actual DOM child of the
        header element (see render_fixed_header()) rather than as a
        separate floating div, so it always inherits the header's
        stacking priority with no z-index tricks needed for the logo
        itself. */
        [data-testid="stHeader"]{
            position: fixed !important;
            top: 0 !important;
            left: 0 !important;
            right: 0 !important;
            width: 100% !important;
            margin: 0 !important;
            background-color: var(--eb-navy) !important;
            height: var(--eb-header-height) !important;
            z-index: 1000000 !important;
        }
        [data-testid="stDecoration"]{
            display: none !important;
        }
        [data-testid="stToolbar"]{
            background: transparent !important;
        }
        .eb-header-logo{
            position: absolute;
            top: 50%;
            left: 3.25rem;
            transform: translateY(-50%);
            display: flex;
            align-items: center;
        }
        .eb-header-logo img{
            height: 3.4rem;
            width: auto;
        }
        .eb-header-logo .eb-logo-placeholder{
            font-family:'JetBrains Mono', monospace;
            font-size:1.1rem;
            font-weight:600;
            letter-spacing:.12em;
            text-transform:uppercase;
            color: var(--eb-bg);
            opacity:0.8;
        }

        /* ---- Header/sidebar layout separation ----
        The sidebar is pinned to start below the header (not at the very
        top of the viewport) and its height is capped to the remaining
        space, so the two occupy separate, non-overlapping layout areas —
        the sidebar can never be positioned over the header/logo, with or
        without a z-index conflict.

        The sidebar's background is a dedicated light gray (--eb-sidebar-bg,
        a tint of the muted-gray brand color), deliberately distinct from
        the white main-content background, applied consistently across the
        sidebar shell and its inner content wrapper so there's no visible
        seam between them.

        The "reopen sidebar" control that Streamlit shows when the sidebar
        is collapsed used to have its own dedicated test id,
        `stSidebarCollapsedControl` — that id no longer exists in this
        Streamlit version, so a selector targeting it matches nothing at
        all (which is why changing `top` on it had zero effect: the rule
        was never being applied to any element in the DOM). The button now
        renders as `[data-testid="stExpandSidebarButton"]`, laid out
        *inside* the header's own flex row (vertically centered by the
        header, not placed via a `top` offset) — that's why it appears
        mid-header, overlapping the logo. `position: fixed` is forced here
        first so it's pulled out of that flex row entirely, then the
        explicit `top`/`left` placement below takes effect, pinning it
        just under the header — the same height whether the sidebar is
        expanded or collapsed. */

        section[data-testid="stSidebar"] div[data-testid="stSidebarHeader"]{
            min-height: 0 !important;
            
            margin-bottom: 0 !important;
        }

        section[data-testid="stSidebar"]{
            top: var(--eb-header-height) !important;
            height: calc(100vh - var(--eb-header-height)) !important;
            background-color: var(--eb-sidebar-bg) !important;
        }
        section[data-testid="stSidebar"] > div{
            background-color: var(--eb-sidebar-bg) !important;
        }
        [data-testid="stExpandSidebarButton"]{
            position: fixed !important;
            top: calc(var(--eb-header-height) + 0.75rem) !important;
            left: 0.75rem !important;
            z-index: 1000001 !important;
        }
        
        section.main > div { padding-top: calc(var(--eb-header-height) + 1.25rem); }

        code, .eb-mono { font-family: 'JetBrains Mono', monospace !important; }

        /* ---- Remove auto-generated heading anchor/link icons ----
        Streamlit automatically turns every markdown heading (#, ##, ###,
        ####, #####) plus st.header/st.subheader/st.title into a clickable
        anchor and renders a chain-link icon beside it. This app doesn't use
        deep-linking to sections, so the icon is just visual noise next to
        every heading, section title, and label — hide it everywhere. */
        [data-testid="stHeaderActionElements"],
        [data-testid="stHeaderActionButton"],
        .stHeadingActionElements{
            display: none !important;
        }

        /* ---- Output table toolbar: "Columns" popover + "Download Excel" ----
        Compact, white, light-border, rounded "toolbar button" look for both
        the Columns popover trigger and the Download Excel button, and a
        card-style popover panel (white, bordered, shadowed, fixed width and
        height) for the show/hide column list. Scoped to the toolbar row via
        the "st-key-output_table_toolbar" hook so nothing else in the app is
        affected. The two buttons live directly inside that keyed container
        (no st.columns nesting — that extra layout nesting is what was
        throwing off the popover's anchor position), and are turned into a
        single right-aligned, tightly-packed flex row here. */

        /* Make "Set Format" and "Select Page Range" cards equal height.
        A fixed min-height is fragile — it only guarantees a floor, so if
        either card's natural content (e.g. help text, wrapped labels)
        grows past that number, the two drift apart again. This instead
        stretches both columns to match whichever card is naturally
        tallest, via ordinary flexbox: the row is a flex container, each
        column is told to stretch full-height and become a flex column
        itself, and the border-wrapper inside each one is stretched to
        fill that column — so the two cards always match exactly, no
        matter what's inside them. */
        div[class*="st-key-sidebar_format_page_row"] div[data-testid="stHorizontalBlock"]{
            align-items: stretch !important;
        }
        div[class*="st-key-sidebar_format_page_row"] div[data-testid="stColumn"]{
            display: flex !important;
        }
        div[class*="st-key-sidebar_format_page_row"] div[data-testid="stColumn"] > div{
            display: flex !important;
            flex-direction: column !important;
            width: 100% !important;
        }
        div[class*="st-key-sidebar_format_page_row"] div[data-testid="stVerticalBlockBorderWrapper"]{
            height: 100% !important;
            display: flex !important;
            flex-direction: column !important;
        }
        div[class*="st-key-sidebar_format_page_row"] div[data-testid="stVerticalBlockBorderWrapper"] > div{
            height: 100% !important;
            flex: 1 !important;
        }

        /* ---- Output table toolbar ----
        Built as a 2-column Streamlit row (st.columns), not nested keyed
        containers — that approach (two custom flex containers relying on
        CSS specificity/cascade order to behave differently from each
        other) turned out fragile: the left group and the row itself
        fought over which `justify-content` won, and the layout broke
        (Download wrapped onto its own line, the dropdown floated off to
        the right on its own). Streamlit's own column layout already does
        the "two independent flex regions side by side" job reliably, so
        this just leans on stHorizontalBlock/stColumn directly: the left
        column holds the Columns popover + Code/Qualifier dropdown packed
        together and left-aligned, the right column holds Download,
        pinned to its own right edge — so the gap between them scales with
        the row's width instead of being a fixed, fragile number. */
        div[class*="st-key-output_table_toolbar"] [data-testid="stHorizontalBlock"]{
            align-items:center !important;
            gap:0.6rem !important;
        }
        div[class*="st-key-output_table_toolbar"] [data-testid="stColumn"]:first-of-type [data-testid="stVerticalBlock"]{
            display:flex !important;
            flex-direction:row !important;
            justify-content:flex-start !important;
            align-items:center !important;
            gap:0.5rem !important;
        }
        div[class*="st-key-output_table_toolbar"] [data-testid="stColumn"]:last-of-type [data-testid="stVerticalBlock"]{
            display:flex !important;
            justify-content:flex-end !important;
        }
        div[class*="st-key-output_table_toolbar"] [data-testid="stElementContainer"]{
            width:auto !important;
            flex:0 0 auto !important;
        }
        div[class*="st-key-output_table_toolbar"] [data-testid="stPopover"] > div > button,
        div[class*="st-key-output_table_toolbar"] .stDownloadButton button{
            background:#FFFFFF !important;
            border:1px solid var(--eb-border) !important;
            border-radius:8px !important;
            color:var(--eb-text) !important;
            font-weight:600 !important;
            font-size:0.85rem !important;
            padding:0.35rem 0.85rem !important;
            min-height:2.1rem !important;
            box-shadow:none !important;
            white-space:nowrap !important;
        }
        div[class*="st-key-output_table_toolbar"] [data-testid="stPopover"] > div > button:hover,
        div[class*="st-key-output_table_toolbar"] .stDownloadButton button:hover{
            border-color:var(--eb-primary) !important;
            color:var(--eb-primary) !important;
        }
        /* Code/Qualifier display dropdown: same compact "toolbar button"
        footprint as the Columns popover trigger and Download button
        alongside it, instead of Streamlit's default full-width select. */
        div[class*="st-key-output_table_toolbar"] [data-testid="stColumn"]:first-of-type div[data-testid="stSelectbox"]{
            width:auto !important;
        }
        div[class*="st-key-output_table_toolbar"] div[data-baseweb="select"] > div{
            background:#FFFFFF !important;
            border:1px solid var(--eb-border) !important;
            border-radius:8px !important;
            min-height:2.1rem !important;
            box-shadow:none !important;
        }
        div[class*="st-key-output_table_toolbar"] div[data-baseweb="select"] > div:hover{
            border-color:var(--eb-primary) !important;
        }
        div[class*="st-key-output_table_toolbar"] div[data-baseweb="select"] *{
            color:var(--eb-text) !important;
            font-weight:600 !important;
            font-size:0.85rem !important;
            white-space:nowrap !important;
        }
        div[data-testid="stPopoverBody"]{
            width:300px !important;
            max-height:420px !important;
            overflow-y:auto !important;
            border-radius:12px !important;
            border:1px solid var(--eb-border) !important;
            box-shadow:0 12px 32px rgba(17,21,28,0.14) !important;
        }
        .eb-colpanel-header{
            display:flex;
            align-items:center;
            gap:0.55rem;
            margin-bottom:0.65rem;
        }
        .eb-colpanel-icon{
            display:flex;
            align-items:center;
            justify-content:center;
            width:26px;
            height:26px;
            border-radius:7px;
            background:var(--eb-bg);
            border:1px solid var(--eb-border);
            flex-shrink:0;
            font-size:0.85rem;
        }
        .eb-colpanel-title{
            font-weight:700;
            font-size:0.92rem;
            color:var(--eb-text);
        }


        /* ---- Header / hero ---- */
        .eb-eyebrow{
            font-family:'JetBrains Mono', monospace;
            font-size:0.72rem;
            letter-spacing:.14em;
            text-transform:uppercase;
            color: var(--eb-primary);
            font-weight:600;
            margin-bottom:.35rem;
        }
        .eb-title{
            font-size:2rem;
            font-weight:800;
            color:var(--eb-text);
            margin:0 0 .15rem 0;
            letter-spacing:-0.01em;
        }
        .eb-subtitle{
            color:var(--eb-text-muted);
            font-size:0.98rem;
            margin-bottom:0.4rem;
        }
        .eb-chip{
            display:inline-block;
            font-family:'JetBrains Mono', monospace;
            font-size:0.74rem;
            font-weight:600;
            color:var(--eb-primary);
            background:var(--eb-primary-soft);
            border:1px solid var(--eb-primary-border);
            border-radius:999px;
            padding:0.18rem 0.65rem;
            margin-top:0.35rem;
        }

        /* ---- Stepper ---- */
        .stepper-wrap{
            display:flex;
            align-items:flex-start;
            justify-content:space-between;
            margin: 0.9rem 0 0.4rem 0;
            padding: 1.1rem 0.6rem 0.4rem 0.6rem;
        }
        .step{
            display:flex;
            flex-direction:column;
            align-items:center;
            flex:1;
            position:relative;
            text-align:center;
            padding:0 6px;
        }
        .step-line{
            position:absolute;
            top:17px;
            left:50%;
            width:100%;
            height:3px;
            background:var(--eb-border);
            z-index:1;
            border-radius:2px;
        }
        .step:last-child .step-line{ display:none; }
        .step.done .step-line{ background:var(--eb-navy); }
        .step-circle{
            width:34px;height:34px;border-radius:50%;
            display:flex;align-items:center;justify-content:center;
            font-weight:700;font-size:0.82rem;
            border:2px solid var(--eb-border);
            background:#fff;color:var(--eb-text-muted);
            position:relative;z-index:2;
            transition: all .2s ease;
        }
        .step.done .step-circle{
            background:var(--eb-navy);
            border-color:var(--eb-navy);
            color:#fff;
        }
        .step.active .step-circle{
            border-color:var(--eb-primary);
            color:var(--eb-primary);
            box-shadow:0 0 0 4px var(--eb-primary-soft);
            background:#fff;
        }
        .step-label{
            margin-top:0.45rem;
            font-size:0.78rem;
            font-weight:600;
            color:var(--eb-text);
        }
        .step.pending .step-label{ color: var(--eb-text-muted); }

        /* ---- Cards ---- */
        div[data-testid="stVerticalBlockBorderWrapper"]{
            border-radius:16px !important;
            border:1px solid var(--eb-border) !important;
            background:var(--eb-card);
            box-shadow:0 1px 2px rgba(16,24,40,0.04), 0 1px 8px rgba(16,24,40,0.03);
        }
        div[data-testid="stVerticalBlockBorderWrapper"] > div { padding:0.2rem 0.1rem; }

        /* ---- Buttons ---- */
        .stButton button{
            border-radius:10px;
            font-weight:600;
            border:1px solid var(--eb-border);
            transition: all .15s ease;
        }
        .stButton button[data-testid="stBaseButton-primary"]{
            background:linear-gradient(135deg, var(--eb-primary), var(--eb-navy));
            border:none;
            box-shadow:0 2px 10px var(--eb-primary-border);
        }
        .stButton button[data-testid="stBaseButton-primary"]:hover{
            box-shadow:0 4px 16px rgba(166,33,38,0.42);
            transform:translateY(-1px);
        }
        .stDownloadButton > button{
            border-radius:10px;
            font-weight:600;
        }

        /* ---- Disabled buttons ----
        Streamlit marks a disabled button with aria-disabled="true" (it does
        NOT use the native disabled attribute), and its own default styling
        only fades the label text — the background stays exactly as-is. This
        overrides that so a disabled button is unmistakably non-clickable:
        muted gray background, muted text, no shadow. */
        .stButton button[aria-disabled="true"],
        .stButton button:disabled{
            background:rgba(172,176,176,0.30) !important;
            background-image:none !important;
            color:rgba(17,20,38,0.38) !important;
            border:1px solid rgba(172,176,176,0.6) !important;
            box-shadow:none !important;
            cursor:not-allowed !important;
        }
        .stButton button[aria-disabled="true"]:hover,
        .stButton button:disabled:hover{
            background:rgba(172,176,176,0.30) !important;
            box-shadow:none !important;
            transform:none !important;
        }

        /* ---- Hover / focus border ----
        Streamlit's default hover AND focus state colors the input border
        with its theme red (applied via dynamically-swapped atomic classes,
        not a plain :hover/:focus rule) — override it with the indigo
        accent everywhere, on both hover and focus: text inputs, number
        inputs (sidebar page range), selectboxes (sidebar document format),
        and textareas. */
        div[data-testid="stTextInputRootElement"]:hover,
        div[data-testid="stTextInputRootElement"]:focus-within,
        div[data-testid="stNumberInputContainer"]:hover,
        div[data-testid="stNumberInputContainer"]:focus-within,
        div[data-baseweb="input"]:hover,
        div[data-baseweb="input"]:focus-within,
        div[data-baseweb="textarea"]:hover,
        div[data-baseweb="textarea"]:focus-within,
        div[data-baseweb="select"]:hover,
        div[data-baseweb="select"]:focus-within,
        div[data-baseweb="select"] > div:hover,
        div[data-baseweb="select"] > div:focus-within{
            border-color: var(--eb-primary) !important;
        }

        /* Sidebar selectbox/number-input retain a focus ring in the same
        red by default too -- neutralize it to match. */
        div[data-baseweb="select"]:focus-within,
        div[data-baseweb="base-input"]:focus-within{
            box-shadow: 0 0 0 1px var(--eb-primary) !important;
        }

        /* ---- st.error() boxes ----
        Streamlit's built-in st.error() uses its own native red theme
        (background, left border, icon) independent of our custom classes.
        The new palette has no separate "danger" color, so both st.error()
        and st.warning() are recolored to use the brand accent red as the
        single caution/danger color. Covers both the current and a couple
        of older Streamlit DOM patterns, since the exact markup has changed
        across versions.

        IMPORTANT — this used to also paint `stAlertContentError` (and the
        Warning/Success siblings below) with the same background as its
        own parent, `stAlertContainer`. In the current Streamlit DOM,
        stAlertContent* is nested *inside* stAlertContainer, not a sibling
        or an alternate pattern — so both were painting the exact same
        translucent color on top of each other, stacking the opacity and
        showing up as a visibly darker rectangle exactly the size of the
        inner content (that's the "double background" in the warning/
        status boxes). Background now lives only on the outer container;
        the nested content selector below only sets text color. */
        div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentError"]),
        div[data-testid="stAlert"][kind="error"],
        div[data-baseweb="notification"][kind="negative"]{
            background-color: var(--eb-primary-soft) !important;
            border-color: var(--eb-primary-border) !important;
            color: var(--eb-primary) !important;
        }
        div[data-testid="stAlertContentError"]{
            color: var(--eb-primary) !important;
        }
        div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentError"]) svg,
        div[data-testid="stAlertContentError"] svg,
        div[data-testid="stAlert"][kind="error"] svg,
        div[data-baseweb="notification"][kind="negative"] svg{
            fill: var(--eb-primary) !important;
        }

        /* st.warning() boxes get the same red-accent treatment, so every
        caution/validation message in the app reads consistently. Same
        nested-nesting note as st.error() above applies here. */
        div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentWarning"]),
        div[data-testid="stAlert"][kind="warning"],
        div[data-baseweb="notification"][kind="warning"]{
            background-color: var(--eb-primary-soft) !important;
            border-color: var(--eb-primary-border) !important;
            color: var(--eb-primary) !important;
        }
        div[data-testid="stAlertContentWarning"]{
            color: var(--eb-primary) !important;
        }
        div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentWarning"]) svg,
        div[data-testid="stAlertContentWarning"] svg,
        div[data-testid="stAlert"][kind="warning"] svg,
        div[data-baseweb="notification"][kind="warning"] svg{
            fill: var(--eb-primary) !important;
        }

        /* st.success() boxes (used for "Excellent"/"Good" extraction
        status) are recolored from Streamlit's default green to the brand
        navy, so no color outside the five-color palette appears anywhere
        in the app. Same nested-nesting note as st.error() above applies
        here — this is the exact box in the "Status: Excellent" screenshot
        that showed a darker inset panel before this fix. */
        div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentSuccess"]),
        div[data-testid="stAlert"][kind="success"],
        div[data-baseweb="notification"][kind="positive"]{
            background-color: rgba(26,30,64,0.06) !important;
            border-color: rgba(26,30,64,0.30) !important;
            color: var(--eb-navy) !important;
        }
        div[data-testid="stAlertContentSuccess"]{
            color: var(--eb-navy) !important;
        }
        div[data-testid="stAlertContainer"]:has(div[data-testid="stAlertContentSuccess"]) svg,
        div[data-testid="stAlertContentSuccess"] svg,
        div[data-testid="stAlert"][kind="success"] svg,
        div[data-baseweb="notification"][kind="positive"] svg{
            fill: var(--eb-navy) !important;
        }

        /* ---- Number input step (-/+) buttons ----
        This one isn't a duplicate CSS rule like the alerts above — it's
        Streamlit's own built-in hover/focus style for these buttons,
        which fills them with the theme's full-strength primary color
        (solid, saturated red) the moment they're hovered *or* focused
        (clicking one leaves it focused). Against this app's much softer
        palette that reads the same as the alert bug: a hard-edged,
        over-saturated patch popping out of an otherwise light control.
        Toning it down to the same soft tint used everywhere else keeps
        the whole app's "highlighted" look consistent. */
        button[data-testid="stNumberInputStepDown"]:hover:enabled,
        button[data-testid="stNumberInputStepDown"]:focus:enabled,
        button[data-testid="stNumberInputStepUp"]:hover:enabled,
        button[data-testid="stNumberInputStepUp"]:focus:enabled{
            background-color: var(--eb-primary-soft) !important;
            color: var(--eb-primary) !important;
        }

        /* ---- File uploader ---- */
        div[data-testid="stFileUploaderDropzone"]{
            border-radius:14px;
            border:1.5px dashed var(--eb-border);
            background:var(--eb-bg);
        }

        /* ---- Expander ---- */
        div[data-testid="stExpander"]{
            border-radius:12px !important;
            border:1px solid var(--eb-border) !important;
        }

        /* ---- Metrics ---- */
        div[data-testid="stMetric"]{
            background:var(--eb-bg);
            border:1px solid var(--eb-border);
            border-radius:12px;
            padding:0.7rem 0.9rem;
        }

        /* ---- Dataframe / data editor ---- */
        div[data-testid="stDataFrame"], div[data-testid="stDataFrameResizable"]{
            border-radius:12px;
            overflow:hidden;
            border:1px solid var(--eb-border);
        }

        /* ---- Progress bar ----
        Streamlit renders st.progress() as:
          div[data-testid="stProgress"]
            > div (role="progressbar")          <- semantic wrapper only
              > div[data-testid="stProgressBarTrack"]   <- the TRACK (full width, always visible)
                > div                                    <- the FILL (slides in via translateX)
        The fill is the part that actually represents "completed", so it
        needs the strong/branded color; the track is the "incomplete"
        background and should stay clearly muted by comparison. */
        div[data-testid="stProgressBarTrack"]{
            background-color: rgba(172,176,176,0.35) !important;
            border-radius:999px !important;
            overflow:hidden;
        }
        div[data-testid="stProgressBarTrack"] > div{
            background-color: var(--eb-primary) !important;
            background-image: linear-gradient(90deg, var(--eb-primary), var(--eb-navy)) !important;
            border-radius:999px !important;
        }

        /* ---- Section captions ---- */
        .eb-hint{
            font-size:0.82rem;
            color:var(--eb-text-muted);
            margin-top:-0.3rem;
            margin-bottom:0.4rem;
        }

        /* ---- Sidebar workflow (widened, resizable, scrollable) ----
        The sidebar now holds all 5 workflow steps. Streamlit's sidebar is
        already natively resizable by dragging its right edge — the
        section renders as a resizable wrapper with its own inline
        `width`, and normally clamps between a small default min/max. The
        previous CSS pinned `width`/`max-width` to a fixed `40vw` with
        `!important`, which overrides that inline width outright and is
        exactly why dragging the handle had no visible effect: the browser
        was never allowed to render anything but 40vw. Setting only
        `min-width`/`max-width` here (no fixed `width`) leaves the native
        drag-resize working — the browser clamps the resizable library's
        inline width to this range automatically, so the sidebar stays
        resizable between 20vw and 45vw, defaulting to whatever width
        Streamlit's own resize state starts at (clamped up to 20vw if it
        would otherwise be narrower). This also leaves the fixed header,
        main content padding, and the collapsed-sidebar control above
        untouched, since none of them read the sidebar's width. */
        

        /* Fixed sidebar width: 40% of screen */
        section[data-testid="stSidebar"][aria-expanded="true"]{
            width: 40vw !important;
            min-width: 40vw !important;
            max-width: 40vw !important;
            flex: 0 0 40vw !important;
            resize: none !important;
        }

        /* Keep the inner sidebar wrapper the same fixed width */
        section[data-testid="stSidebar"][aria-expanded="true"] > div{
            width: 40vw !important;
            min-width: 40vw !important;
            max-width: 40vw !important;
            resize: none !important;
        }

        /* Hide Streamlit sidebar dragging / resize handle */
        div[data-testid="stSidebarResizeHandle"],
        div[data-testid="stSidebarResizer"],
        div[data-testid*="SidebarResize"],
        div[data-testid*="SidebarResizer"]{
            display: none !important;
            visibility: hidden !important;
            opacity: 0 !important;
            width: 0 !important;
            min-width: 0 !important;
            max-width: 0 !important;
            pointer-events: none !important;
        }

        /* Remove resize cursor near the sidebar border */
        section[data-testid="stSidebar"],
        section[data-testid="stSidebar"] *,
        section[data-testid="stSidebar"] + div{
            cursor: default !important;
        }

        section[data-testid="stSidebar"] div[data-testid="stSidebarContent"]{
            height: 100%;
            overflow-y: auto;
            background-color: var(--eb-sidebar-bg) !important;
            /* Left/right breathing space from the sidebar's own edges. */
            padding-left: 1.875rem !important;
            padding-right: 1.875rem !important;
        }
        section[data-testid="stSidebar"] div[data-testid="stSidebarUserContent"]{
            padding-top: 0.1rem !important;
            padding-bottom: 1.725rem !important;
        }

        /* ---- Sidebar step boxes ----
        Each of the 5 workflow steps sits in its own bordered container
        (st.container(border=True)) inside the sidebar. These get a
        noticeably thicker, higher-contrast border than the cards
        elsewhere in the app, plus generous inner padding, so each step
        reads as a clearly separated block in the widened sidebar.
        NOTE: this padding is on each individual step card, not the
        sidebar's own edges — it doesn't affect the sidebar's left/right/
        top/bottom breathing space (see stSidebarContent/stSidebarUserContent
        above for that). */
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlockBorderWrapper"]{
            border-width: 2px !important;
            border-style: solid !important;
            border-color: var(--eb-border-strong) !important;
            margin-bottom: 1rem;
        }
        section[data-testid="stSidebar"] div[data-testid="stVerticalBlockBorderWrapper"] > div{
            padding: 1.725rem 1.875rem !important;
        }

        /* ---- Sidebar step title (circled step number + label) ----
        Reuses the stepper's circle look (accent circle, number centered)
        as a small inline icon in front of each of the 5 sidebar section
        titles, so the sidebar reads as the same 5-step flow as the
        stepper on the right. */
        .eb-step-title{
            display:flex;
            align-items:center;
            gap:0.55rem;
            font-weight:700;
            font-size:1rem;
            color:var(--eb-text);
            margin: 0.3rem 0 0.6rem 0;
        }
        .eb-step-title:first-child{ margin-top:0.2rem; }
        .eb-step-circle-icon{
            display:flex;
            align-items:center;
            justify-content:center;
            flex-shrink:0;
            width:26px;height:26px;
            border-radius:50%;
            border:2px solid var(--eb-primary);
            color:var(--eb-primary);
            background:#fff;
            font-weight:700;
            font-size:0.78rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_fixed_header() -> None:
    """Insert the company logo into Streamlit's own header bar.

    This deliberately does NOT render a separate `position: fixed` div via
    st.markdown — that approach puts the logo in its own stacking context
    that can end up rendered underneath the sidebar (the bug this replaces).
    Instead, a tiny JS snippet appends the logo as an actual DOM child of
    Streamlit's real header element (`[data-testid="stHeader"]`), so it
    lives inside the header's own subtree and always has header-level
    stacking priority, matching the CSS in inject_global_css() that pins
    the header above the sidebar and the sidebar below the header.

    st.markdown() script tags don't execute in this environment, so the JS
    has to run via st.components.v1.html(..., height=0), reaching the main
    page through window.parent.document (the component itself renders in
    its own iframe).

    If no file exists yet at COMPANY_LOGO_PATH, a plain text placeholder is
    shown instead so the header still renders cleanly — drop your logo
    image at that path (or update the constant) to have it appear here.
    """
    logo_path = Path(COMPANY_LOGO_PATH)
    logo_inner_html = '<span class="eb-logo-placeholder">COMPANY LOGO</span>'

    if logo_path.exists():
        try:
            logo_bytes = logo_path.read_bytes()
            ext = logo_path.suffix.lstrip(".").lower() or "png"
            logo_b64 = base64.b64encode(logo_bytes).decode("utf-8")
            logo_inner_html = f'<img src="data:image/{ext};base64,{logo_b64}" alt="Company logo" />'
        except OSError:
            pass

    # Escape backslashes/backticks so the HTML can sit safely inside a JS
    # template literal.
    safe_html = logo_inner_html.replace("\\", "\\\\").replace("`", "\\`")

    components.html(
        f"""
        <script>
        (function() {{
            var doc = window.parent.document;
            var header = doc.querySelector('[data-testid="stHeader"]');
            if (!header) {{ return; }}

            var existing = header.querySelector('.eb-header-logo');
            if (existing) {{ existing.remove(); }}

            var wrapper = doc.createElement('div');
            wrapper.className = 'eb-header-logo';
            wrapper.innerHTML = `{safe_html}`;
            header.appendChild(wrapper);
        }})();
        </script>
        """,
        height=0,
    )


def render_sample_pdf_viewer(pdf_path: str, height: int = 420) -> None:
    """Render an inline preview of a sample PDF, or a friendly note if missing."""
    path = Path(pdf_path)
    if not path.exists():
        st.caption(
            f"No sample file found yet — add one at `{pdf_path}` to show a "
            "preview here."
        )
        return

    try:
        pdf_bytes = path.read_bytes()
    except OSError as exc:
        st.caption(f"Could not read sample file `{pdf_path}` ({exc}).")
        return

    b64 = base64.b64encode(pdf_bytes).decode("utf-8")
    st.markdown(
        f'<iframe src="data:application/pdf;base64,{b64}" width="100%" '
        f'height="{height}" style="border-radius:12px; border:1px solid '
        f'var(--eb-border);"></iframe>',
        unsafe_allow_html=True,
    )
    st.download_button(
        "Download",
        data=pdf_bytes,
        file_name=path.name,
        mime="application/pdf",
        key=f"sample_dl_{path.name}",
    )


def render_sample_panel(label: str, document_type: str) -> None:
    """Render the 'Sample <format>' card with a collapsible PDF preview."""
    sample_path = SAMPLE_PDF_PATHS.get(document_type, "")
    expander_key = f"sample_expanded__{document_type.replace(' ', '_')}"
    default_expanded = expander_key not in st.session_state

    with st.container(border=True):
        st.markdown(f"##### {label}")
        st.caption("A reference example of this layout, for orientation.")
        with st.expander("Preview sample PDF", expanded=default_expanded, key=expander_key):
            render_sample_pdf_viewer(sample_path)


def collapse_sample_panel(document_type: str) -> None:
    """Mark the sample panel for this format as collapsed on the next run."""
    expander_key = f"sample_expanded__{document_type.replace(' ', '_')}"
    st.session_state[expander_key] = False


def render_sidebar_step_title(number: int, label: str) -> None:
    """Render a sidebar section title with a circled step number in front of
    it, matching the stepper's circle styling."""
    st.markdown(
        f'<div class="eb-step-title">'
        f'<span class="eb-step-circle-icon">{number}</span>{label}'
        f"</div>",
        unsafe_allow_html=True,
    )


def render_uploaded_pdf_preview(uploaded_file) -> None:
    """Render a collapsed-by-default inline preview of the uploaded PDF."""
    with st.expander("Preview uploaded PDF", expanded=False):
        pdf_bytes = uploaded_file.getvalue()
        b64 = base64.b64encode(pdf_bytes).decode("utf-8")
        st.markdown(
            f'<iframe src="data:application/pdf;base64,{b64}" width="100%" '
            f'height="420" style="border-radius:12px; border:1px solid '
            f'var(--eb-border);"></iframe>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Output table column visibility (toolbar "Columns" dropdown)
# ---------------------------------------------------------------------------

AUDIT_COLUMNS = [
    "source_page",
    "segment_id",
    "segment_name",
    "needs_review",
    "review_reason",
]

DEFAULT_HIDDEN_COLUMNS = ["Description", "Details"]


def _sync_show_all_checkbox() -> None:
    """Keep the 'Show All' checkbox reflecting whether every column is visible."""
    visible = st.session_state.get("col_visible", {})
    if visible:
        st.session_state["show_all_columns"] = all(visible.values())


def _sync_hide_audit_checkbox() -> None:
    """Keep the 'Hide audit columns' checkbox reflecting actual audit-column state."""
    visible = st.session_state.get("col_visible", {})
    present = [c for c in AUDIT_COLUMNS if c in visible]
    if present:
        st.session_state["hide_audit_columns"] = all(
            not visible[c] for c in present
        )


def _on_column_visibility_toggle(col: str) -> None:
    """Handle an individual column checkbox being (un)checked."""
    st.session_state["col_visible"][col] = st.session_state[f"colvis__{col}"]
    _sync_show_all_checkbox()
    _sync_hide_audit_checkbox()


def _on_show_all_toggle() -> None:
    """Handle the 'Show All' checkbox: show everything, or restore prior state."""
    visible = st.session_state.get("col_visible", {})
    if st.session_state.get("show_all_columns"):
        st.session_state["col_visible_snapshot"] = dict(visible)
        for col in visible:
            visible[col] = True
            st.session_state[f"colvis__{col}"] = True
    else:
        snapshot = st.session_state.get("col_visible_snapshot")
        if snapshot:
            for col, was_visible in snapshot.items():
                if col in visible:
                    visible[col] = was_visible
                    st.session_state[f"colvis__{col}"] = was_visible
    _sync_hide_audit_checkbox()


def _on_hide_audit_toggle() -> None:
    """
    Handle the 'Hide audit columns' checkbox.

    Checking it hides source_page/segment_id/segment_name/needs_review/
    review_reason, remembering each one's prior visibility first.
    Unchecking it restores each audit column to whatever it was set to
    right before the checkbox was checked — so a column the user had
    already hidden manually through the column list stays hidden.
    """
    visible = st.session_state.get("col_visible", {})
    present = [c for c in AUDIT_COLUMNS if c in visible]

    if st.session_state.get("hide_audit_columns"):
        st.session_state["col_visible_audit_snapshot"] = {
            c: visible[c] for c in present
        }
        for col in present:
            visible[col] = False
            st.session_state[f"colvis__{col}"] = False
    else:
        snapshot = st.session_state.get("col_visible_audit_snapshot") or {}
        for col in present:
            restored = snapshot.get(col, True)
            visible[col] = restored
            st.session_state[f"colvis__{col}"] = restored

    _sync_show_all_checkbox()


def inject_column_menu_patch() -> None:
    """Strip 'Hide column' from the data editor's built-in ⋮ column menu.

    Streamlit doesn't expose an API to selectively disable individual items
    in that menu, so this patches the rendered DOM directly: it watches for
    the menu being opened and hides any menu row whose label is exactly
    "Hide column", leaving "Autosize" and "Pin column" untouched. Column
    hide/show is handled instead by the "Columns" toolbar dropdown, whose
    state (unlike the built-in menu's) is tracked in Python so it can
    always be reversed.

    This is best-effort DOM patching, not a public Streamlit API — if a
    future Streamlit release changes the column menu's markup, this may
    need updating to match.
    """
    st.markdown(
        """
        <script>
        (function() {
            function hideRow(el) {
                var row = el.closest('li') || el.parentElement;
                if (row) { row.style.display = "none"; }
            }
            function scan(root) {
                if (!root || !root.querySelectorAll) return;
                var nodes = root.querySelectorAll('*');
                for (var i = 0; i < nodes.length; i++) {
                    var n = nodes[i];
                    if (n.children.length === 0 &&
                        n.textContent.trim() === "Hide column") {
                        hideRow(n);
                    }
                }
            }
            var observer = new MutationObserver(function(mutations) {
                mutations.forEach(function(m) {
                    m.addedNodes.forEach(function(n) {
                        if (n.nodeType === 1) { scan(n); }
                    });
                });
            });
            observer.observe(document.body, { childList: true, subtree: true });
        })();
        </script>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Step indicator
# ---------------------------------------------------------------------------

def build_workflow_steps(
    document_type: str,
    uploaded_file,
    page_start: int,
    page_end: int,
    expected_columns: list[str],
    key_column: str | None,
    multiline_column: str | None,
    extraction_done: bool,
) -> list[dict]:
    steps = [
        {"label": "Upload PDF", "done": uploaded_file is not None},
        {"label": "Set format", "done": bool(document_type)},
        {"label": "Select page range", "done": (page_start != 1 or page_end != 1)},
    ]

    # "Other" allows multiline_column to be deliberately None (the user
    # picked "extract as-is, no splitting"), so it isn't required for
    # the step to be considered done there. Formatted 1 and Formatted 2
    # have no such option and always need a real column selected.
    if document_type == "Other":
        columns_ready = bool(expected_columns) and bool(key_column)
    else:
        columns_ready = (
            bool(expected_columns) and bool(key_column) and bool(multiline_column)
        )
    steps.append({"label": "Configure columns", "done": columns_ready})
    steps.append({"label": "Run extraction", "done": extraction_done})

    # A step can only be considered complete if every step before it is
    # also complete. Without this, each step's "done" flag is independent,
    # so e.g. removing the uploaded PDF (which flips "Upload PDF" back to
    # not-done) had no effect on "Run extraction", which stayed "done"
    # forever just because a result from an earlier run was still sitting
    # in session state. This makes completion cascade: the first not-done
    # step, and everything after it, always reads as incomplete.
    all_done_so_far = True
    for step in steps:
        if not all_done_so_far:
            step["done"] = False
        all_done_so_far = all_done_so_far and step["done"]

    return steps


def render_stepper(slot, steps: list[dict]) -> None:
    first_pending_seen = False
    html_parts = ['<div class="stepper-wrap">']

    for i, step in enumerate(steps):
        if step["done"]:
            state = "done"
            icon = "✓"
        elif not first_pending_seen:
            state = "active"
            icon = str(i + 1)
            first_pending_seen = True
        else:
            state = "pending"
            icon = str(i + 1)

        html_parts.append(
            f'<div class="step {state}">'
            f'<div class="step-line"></div>'
            f'<div class="step-circle">{icon}</div>'
            f'<div class="step-label">{step["label"]}</div>'
            f"</div>"
        )

    html_parts.append("</div>")

    with slot.container():
        st.markdown("".join(html_parts), unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Extraction with live progress (UI-only: the backend call itself is
# untouched, it just now runs on a worker thread so the UI can keep
# updating a progress bar / status text while it works).
# ---------------------------------------------------------------------------

def estimate_extraction_seconds(document_type: str, page_start: int, page_end: int) -> int:
    page_count = max(1, int(page_end) - int(page_start) + 1)
    base = 6 + (page_count * 3)
    if document_type == "Other":
        base = int(base * 1.6)
    return max(8, base)


def run_extraction_with_progress(extract_kwargs: dict, estimated_seconds: int):
    """Run extract_tables_from_pdf on a background thread while updating a
    progress bar / status text / elapsed timer in the main thread."""

    result_container = {"df": None, "error": None, "done": False}

    def worker():
        try:
            result_container["df"] = extract_tables_from_pdf(**extract_kwargs)
        except Exception as exc:  # noqa: BLE001
            result_container["error"] = exc
        finally:
            result_container["done"] = True

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()

    status_placeholder = st.empty()
    progress_bar = st.progress(0)
    detail_placeholder = st.empty()

    start_time = time.time()
    n_steps = len(STATUS_STEPS)

    while not result_container["done"]:
        elapsed = time.time() - start_time
        fraction = min(elapsed / estimated_seconds, 0.97)
        step_idx = min(int(fraction * n_steps), n_steps - 1)

        status_placeholder.markdown(f"**{STATUS_STEPS[step_idx]}…**")
        progress_bar.progress(fraction)

        remaining = max(0, estimated_seconds - elapsed)
        detail_placeholder.caption(
            f"Elapsed {int(elapsed)}s · est. {int(remaining)}s remaining · "
            f"{int(fraction * 100)}% complete"
        )

        time.sleep(0.25)

    total_elapsed = time.time() - start_time
    progress_bar.progress(1.0)
    status_placeholder.markdown("**Finished**")
    detail_placeholder.caption(f"Completed in {total_elapsed:.1f}s")

    result_container["elapsed_seconds"] = total_elapsed

    return result_container


# ---------------------------------------------------------------------------
# Page setup
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="EDI Extractor",
    layout="wide",
)

inject_global_css()
inject_column_menu_patch()
render_fixed_header()

# ---------------------------------------------------------------------------
# Sidebar — all 5 workflow steps live here, top to bottom, in order
# ---------------------------------------------------------------------------

with st.sidebar:
    # -- Step 1: Upload PDF -------------------------------------------------
    with st.container(border=True):
        render_sidebar_step_title(1, "Upload PDF")
        uploaded_file = st.file_uploader("Upload PDF", type=["pdf"], label_visibility="collapsed")
        if uploaded_file is not None:
            render_uploaded_pdf_preview(uploaded_file)

    # -- Step 2 + Step 3: Format and Page Range side by side -------------------
    # -- Step 2 + Step 3: Format and Page Range side by side -------------------
    with st.container(key="sidebar_format_page_row"):
        format_col, page_range_col = st.columns([0.9, 1.1])

        with format_col:
            with st.container(border=True, height=155):
                render_sidebar_step_title(2, "Set Format")
                st.caption("Refer to the format samples on the right.")
                document_type = st.selectbox(
                    "Select document format",
                    options=["Formatted 1", "Formatted 2", "Other"],
                    index=0,
                    label_visibility="collapsed",
                    help=(
                        "Formatted 1 is the structured EDI guide format with Segment ID, "
                        "metadata box, and Element Summary table. Other uses the generic "
                        "AI extractor."
                    ),
                )

        with page_range_col:
            with st.container(border=True, height=155):
                render_sidebar_step_title(3, "Select Page Range")
                page_col1, page_col2 = st.columns(2)
                with page_col1:
                    page_start = st.number_input("Start", min_value=1, value=1, step=1)
                with page_col2:
                    page_end = st.number_input("End", min_value=1, value=1, step=1)





    # "Other" mode AI extraction settings (DPI / Azure model) are fixed
    # internally — see OTHER_MODE_DPI / OTHER_MODE_MODEL above — and are no
    # longer exposed in the UI.
    dpi = OTHER_MODE_DPI
    model = OTHER_MODE_MODEL

    # -- Step 4: Configure Columns --------------------------------------------
    # Same "Extraction Settings" logic/defaults as before, per document
    # type — just relocated into the sidebar's numbered flow.
    with st.container(border=True):
        render_sidebar_step_title(4, "Configure Columns")

        if document_type == "Formatted 1":
            include_audit_columns = True

            st.markdown("###### Table columns from the PDF")
            columns_text = st.text_area(
                "Write one column per line",
                value=DEFAULT_FORMATTED1_COLUMNS,
                height=160,
                key="f1_columns_text",
            )
            expected_columns = split_column_text(columns_text)

            if expected_columns:
                key_column = st.selectbox(
                    "Key column: a new record starts when this column has a value",
                    options=expected_columns,
                    index=0,
                    key="f1_key_column",
                    help=(
                        "The column that identifies each element; used to anchor where each record begins."
                    ),
                )

                guessed_multiline_index = 0
                for idx, col in enumerate(expected_columns):
                    if "name" in col.lower():
                        guessed_multiline_index = idx
                        break

                multiline_column = st.selectbox(
                    "Multi-line column containing element name/qualifiers",
                    options=expected_columns,
                    index=guessed_multiline_index,
                    key="f1_multiline_column",
                    help=(
                        "The column with the element name, description, code/name lists listed across multiple lines."
                    ),
                )
            else:
                key_column = ""
                multiline_column = None

            extra_user_instructions = ""

        elif document_type == "Formatted 2":
            include_audit_columns = True

            st.markdown("###### Table columns from the PDF")
            columns_text = st.text_area(
                "Write one column per line",
                value=DEFAULT_FORMATTED2_COLUMNS,
                height=160,
            )
            expected_columns = split_column_text(columns_text)

            if expected_columns:
                key_column = st.selectbox(
                    "Key column: a new record starts when this column has a value",
                    options=expected_columns,
                    index=0,
                    help=(
                        "The column that identifies each element; used to anchor where each record begins."
                    ),
                )

                guessed_multiline_index = 0
                for idx, col in enumerate(expected_columns):
                    if "name" in col.lower():
                        guessed_multiline_index = idx
                        break

                multiline_column = st.selectbox(
                    "Multi-line column containing element name/qualifiers",
                    options=expected_columns,
                    index=guessed_multiline_index,
                    help=(
                        "The column with the element name, description, code/name lists listed across multiple lines."
                    ),
                )
            else:
                key_column = ""
                multiline_column = None

            extra_user_instructions = ""

        else:  # "Other"
            include_audit_columns = True

            st.markdown("###### Table columns from the PDF")
            columns_text = st.text_area(
                "Write one column per line",
                value=DEFAULT_OTHER_COLUMNS,
                height=160,
            )
            expected_columns = split_column_text(columns_text)

            if expected_columns:
                key_column = st.selectbox(
                    "Key column: a new record starts when this column has a value",
                    options=expected_columns,
                    index=0,
                    help=(
                        "The column that identifies each element; used to anchor where each record begins."
                    ),
                )

                no_split_option = "None — extract every column as-is, no splitting"
                multiline_options = [no_split_option] + expected_columns

                guessed_multiline_index = 0
                for idx, col in enumerate(expected_columns):
                    if "name" in col.lower():
                        guessed_multiline_index = idx + 1
                        break

                multiline_selection = st.selectbox(
                    "Multi-line column containing element name/qualifiers",
                    options=multiline_options,
                    index=guessed_multiline_index,
                    help=(
                        "The column with the element name, description, code/name lists listed across multiple lines."
                    ),
                )

                multiline_column = (
                    None if multiline_selection == no_split_option else multiline_selection
                )
            else:
                key_column = ""
                multiline_column = None

            extra_user_instructions = st.text_area(
                "Extra instructions for AI (optional)",
                value=DEFAULT_EXTRA_INSTRUCTIONS,
                height=100,
            )

    # -- Validation ------------------------------------------------------
    validation_errors = []

    if uploaded_file is None:
        validation_errors.append("Please upload a PDF file.")

    if page_end < page_start:
        validation_errors.append("End page must be greater than or equal to start page.")

    if document_type in {"Formatted 1", "Formatted 2", "Other"}:
        if not expected_columns:
            validation_errors.append("Please enter at least one source column.")
        if key_column and key_column not in expected_columns:
            validation_errors.append("Key column must be one of the source columns.")
        if multiline_column and multiline_column not in expected_columns:
            validation_errors.append("Multi-line column must be one of the source columns.")

    # -- Step 5: Run Extraction --------------------------------------------
    with st.container(border=True):
        render_sidebar_step_title(5, "Run Extraction")
        for error in validation_errors:
            st.warning(error)
        run_button = st.button(
            "Run extraction",
            type="primary",
            disabled=bool(validation_errors),
            width="stretch",
        )


# ---------------------------------------------------------------------------
# Main area — title/captions, stepper, sample PDFs, and results
# ---------------------------------------------------------------------------

st.markdown('<div class="eb-eyebrow">EDI 850 · PURCHASE ORDER PARSER</div>', unsafe_allow_html=True)
st.markdown('<div class="eb-title">EDI Extractor</div>', unsafe_allow_html=True)
# st.markdown(
#     '<div class="eb-subtitle">Turn EDI 850 purchase order PDFs into a clean, '
#     "reviewable table — upload, choose your page range, and run the "
#     "extraction.</div>",
#     unsafe_allow_html=True,
# )
st.markdown(f'<span class="eb-chip">{document_type}</span>', unsafe_allow_html=True)

stepper_slot = st.empty()

if document_type == "Formatted 1":
    render_sample_panel("The Sample Format 1", document_type)
elif document_type == "Formatted 2":
    render_sample_panel("The Sample Format 2", document_type)
else:
    render_sample_panel("The Sample Format (Other)", document_type)


# ---------------------------------------------------------------------------
# Stepper render (now that all format-specific settings are known)
# ---------------------------------------------------------------------------

current_file_signature = (
    (uploaded_file.name, uploaded_file.size) if uploaded_file is not None else None
)
extraction_done = (
    current_file_signature is not None
    and "result_df" in st.session_state
    and st.session_state.get("result_df_source") == current_file_signature
)

workflow_steps = build_workflow_steps(
    document_type=document_type,
    uploaded_file=uploaded_file,
    page_start=page_start,
    page_end=page_end,
    expected_columns=expected_columns,
    key_column=key_column,
    multiline_column=multiline_column,
    extraction_done=extraction_done,
)
render_stepper(stepper_slot, workflow_steps)


# ---------------------------------------------------------------------------
# Run extraction
# ---------------------------------------------------------------------------

if run_button and uploaded_file is not None:
    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = Path(tmp_dir) / uploaded_file.name
        with open(pdf_path, "wb") as f:
            f.write(uploaded_file.getbuffer())

        extract_kwargs = {
            "pdf_path": str(pdf_path),
            "page_start": int(page_start),
            "page_end": int(page_end),
            "expected_columns": expected_columns,
            "key_column": key_column,
            "extra_user_instructions": extra_user_instructions,
            "model": model if document_type == "Other" else None,
            "dpi": int(dpi) if document_type == "Other" else 220,
            "document_type": document_type,
            "include_audit_columns": include_audit_columns,
        }

        # All three document types now use expected_columns/key_column; all
        # three also use multiline_column (Formatted 1 splits it into
        # Element Name/Description/Qualifiers/Details, same idea as the
        # other two routes).
        extract_kwargs["multiline_column"] = multiline_column

        est_seconds = estimate_extraction_seconds(document_type, page_start, page_end)

        with st.status("Running extraction…", expanded=True) as status_box:
            result = run_extraction_with_progress(extract_kwargs, est_seconds)

            if result["error"] is not None:
                status_box.update(label="Extraction failed", state="error", expanded=True)
                st.error(f"Extraction failed: {result['error']}")
                st.stop()

            status_box.update(label="Extraction finished", state="complete", expanded=False)

        st.session_state["result_df"] = result["df"]
        st.session_state["result_df_source"] = (uploaded_file.name, uploaded_file.size)
        st.session_state["result_run_info"] = {
            "pdf_filename": uploaded_file.name,
            "extraction_mode": document_type,
            "page_start": int(page_start),
            "page_end": int(page_end),
            "processing_time_seconds": result.get("elapsed_seconds"),
        }
        collapse_sample_panel(document_type)
        st.rerun()


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

if "result_df" in st.session_state:
    result_df = st.session_state["result_df"]

    if result_df is None:
        st.error("Backend returned None instead of a DataFrame.")
        st.stop()

    if not isinstance(result_df, pd.DataFrame):
        st.error(f"Backend returned {type(result_df)}, expected pandas DataFrame.")
        st.stop()

    st.markdown("#### Extraction result")

    if result_df.empty:
        st.warning(
            "No rows were extracted. Try changing page range, source columns, "
            "key column, DPI, or prompt instructions."
        )
    else:
        total_rows = len(result_df)

        if "needs_review" in result_df.columns:
            review_count = int(result_df["needs_review"].fillna(False).astype(bool).sum())
        else:
            review_count = 0

        with st.container(border=True):
            col1, col2, col3 = st.columns(3)
            col1.metric("Rows extracted", total_rows)
            col2.metric("Rows needing review", review_count)
            if total_rows:
                col3.metric("Review ratio", f"{review_count / total_rows:.0%}")
            else:
                col3.metric("Review ratio", "0%")

        # -----------------------------------------
        # Extraction Report
        # -----------------------------------------
        run_info = st.session_state.get("result_run_info", {}) or {}

        extraction_report = build_extraction_report(
            df=result_df,
            pdf_filename=run_info.get("pdf_filename"),
            extraction_mode=run_info.get("extraction_mode"),
            page_start=run_info.get("page_start"),
            page_end=run_info.get("page_end"),
            processing_time_seconds=run_info.get("processing_time_seconds"),
        )

        render_extraction_report(extraction_report)

        # -----------------------------------------
        # Rows needing review section
        # -----------------------------------------
        if "needs_review" in result_df.columns:
            review_mask = result_df["needs_review"].fillna(False).astype(bool)
        else:
            review_mask = pd.Series([False] * len(result_df))

        review_df = result_df[review_mask].copy()

        if not review_df.empty:
            with st.container(border=True):
                st.markdown("##### Rows needing review")
                st.warning(
                    "Please review these rows before "
                    "downloading the final file."
                )
                st.dataframe(
                    review_df.style.apply(highlight_review_rows, axis=1),
                    width="stretch",
                    height=250,
                )

        # -----------------------------------------
        # Editable final table
        # -----------------------------------------
        with st.container(border=True):
            st.markdown("##### Editable extraction table")

            # Post-process "Qualifiers" -> "Qualifiers / Code Values" plus its
            # split-out columns, then keep only whichever of the merged/split
            # columns belong to the currently-selected display mode (read from
            # session_state before the mode selectbox itself is instantiated
            # further down in the toolbar — by the time this script reruns,
            # Streamlit has already updated session_state from the widget's
            # last interaction, so this reflects the user's current choice).
            qualifier_display_mode = st.session_state.get(
                "qualifier_display_mode", DEFAULT_QUALIFIER_DISPLAY_MODE
            )
            final_output_df = split_qualifiers_code_values_column(result_df)
            final_output_df = apply_qualifier_display_mode(
                final_output_df, qualifier_display_mode
            )
            all_columns = list(final_output_df.columns)

            # Reset column-visibility state whenever the column set changes
            # (e.g. a new extraction run against a different vendor/format).
            if st.session_state.get("col_visible_columns_source") != all_columns:
                st.session_state["col_visible_columns_source"] = all_columns
                st.session_state["col_visible"] = {
                    c: c not in DEFAULT_HIDDEN_COLUMNS for c in all_columns
                }
                st.session_state["col_visible_snapshot"] = None
                st.session_state["col_visible_audit_snapshot"] = None
                st.session_state["show_all_columns"] = not any(
                    c in all_columns for c in DEFAULT_HIDDEN_COLUMNS
                )
                st.session_state["hide_audit_columns"] = False
                st.session_state["col_search_query"] = ""
                for c in all_columns:
                    st.session_state[f"colvis__{c}"] = c not in DEFAULT_HIDDEN_COLUMNS

            with st.container(key="output_table_toolbar"):
                left_col, right_col = st.columns([3, 1])
                with left_col:
                    with st.popover(":material/visibility: Columns"):
                        st.markdown(
                            '<div class="eb-colpanel-header">'
                            '<div class="eb-colpanel-title">Show / Hide Columns</div>'
                            "</div>",
                            unsafe_allow_html=True,
                        )

                        search_query = st.text_input(
                            "Search columns",
                            key="col_search_query",
                            placeholder="Search in columns...",
                            label_visibility="collapsed",
                        )

                        st.checkbox(
                            "Show All",
                            key="show_all_columns",
                            on_change=_on_show_all_toggle,
                        )

                        st.checkbox(
                            "Hide audit columns",
                            key="hide_audit_columns",
                            on_change=_on_hide_audit_toggle,
                        )

                        # st.divider()

                        query = search_query.strip().lower()
                        matching_columns = (
                            [c for c in all_columns if query in c.lower()]
                            if query
                            else all_columns
                        )

                        if not matching_columns:
                            st.caption("No columns match your search.")
                        else:
                            # Header/search/Show All stay put; only this inner
                            # fixed-height container scrolls when there are many
                            # columns.
                            with st.container(height=220):
                                for col in matching_columns:
                                    st.checkbox(
                                        col,
                                        key=f"colvis__{col}",
                                        on_change=_on_column_visibility_toggle,
                                        args=(col,),
                                    )

                    st.selectbox(
                        "Code/Qualifier display",
                        options=QUALIFIER_DISPLAY_MODES,
                        index=QUALIFIER_DISPLAY_MODES.index(DEFAULT_QUALIFIER_DISPLAY_MODE),
                        key="qualifier_display_mode",
                        label_visibility="collapsed",
                        format_func=lambda opt: f"Code/Qualifier display: {opt}",
                    )

                with right_col:
                    download_slot = st.empty()

            visible_columns = [
                c for c in all_columns if st.session_state["col_visible"].get(c, True)
            ]
            if not visible_columns:
                st.warning("Select at least one column to display and export.")
                visible_columns = all_columns

            edited_df = st.data_editor(
                final_output_df,
                column_order=visible_columns,
                width="stretch",
                height=500,
                num_rows="dynamic",
                hide_index=False,
                key="final_editable_table",
            )

            export_df = edited_df[visible_columns]
            excel_bytes = dataframe_to_excel_bytes_with_report(export_df, extraction_report)

            download_slot.download_button(
                ":material/download: Download Excel",
                data=excel_bytes,
                file_name="edi_ai_extraction_result.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
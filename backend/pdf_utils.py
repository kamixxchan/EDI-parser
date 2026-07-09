from pathlib import Path
import fitz  # PyMuPDF


def pdf_pages_to_images(
    pdf_path: str,
    output_dir: str,
    page_start: int,
    page_end: int,
    dpi: int = 220,
) -> list[dict]:
    """
    Convert selected PDF pages to PNG images.

    Returns:
        [
            {"page_number": 1, "image_path": ".../page_001.png"},
            {"page_number": 2, "image_path": ".../page_002.png"}
        ]
    """

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    page_images = []

    zoom = dpi / 72
    matrix = fitz.Matrix(zoom, zoom)

    with fitz.open(pdf_path) as doc:
        total_pages = len(doc)

        safe_start = max(1, page_start)
        safe_end = min(page_end, total_pages)

        for page_number in range(safe_start, safe_end + 1):
            page = doc[page_number - 1]

            pix = page.get_pixmap(matrix=matrix, alpha=False)

            image_path = output_path / f"page_{page_number:03d}.png"
            pix.save(str(image_path))

            page_images.append(
                {
                    "page_number": page_number,
                    "image_path": str(image_path),
                }
            )

    return page_images
from __future__ import annotations

import os
from io import BytesIO

from pypdf import PdfReader


MAX_PDF_PAGES = 100


class DocumentParseError(ValueError):
    """Raised when a PDF cannot be converted into readable answer text."""


def extract_pdf_text(pdf_bytes: bytes) -> tuple[str, str]:
    """Extract embedded PDF text, falling back to local OCR for scanned pages.

    Returns (text, extraction_method). OCR uses the installed Tesseract binary;
    the Python wrapper and PyMuPDF are declared in requirements.txt.
    """
    try:
        reader = PdfReader(BytesIO(pdf_bytes), strict=False)
        if reader.is_encrypted:
            raise DocumentParseError("This PDF is password protected. Upload an unlocked PDF.")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise DocumentParseError(f"PDFs may contain at most {MAX_PDF_PAGES} pages.")
        if not reader.pages:
            raise DocumentParseError("This PDF does not contain any pages.")
        pages = [(page.extract_text() or "").strip() for page in reader.pages]
    except DocumentParseError:
        raise
    except Exception as exc:
        raise DocumentParseError(f"Could not read this PDF: {exc}") from exc

    digital_text = "\n\n".join(page for page in pages if page)
    pages_needing_ocr = {index for index, page_text in enumerate(pages) if len(page_text) < 25}
    if not pages_needing_ocr:
        return digital_text.strip(), "embedded_text"

    try:
        import fitz
        import pytesseract
        from PIL import Image

        tesseract_cmd = os.getenv("TESSERACT_CMD", "").strip()
        if tesseract_cmd:
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd

        document = fitz.open(stream=pdf_bytes, filetype="pdf")
        if document.page_count > MAX_PDF_PAGES:
            raise DocumentParseError(f"PDFs may contain at most {MAX_PDF_PAGES} pages.")
        combined_pages = []
        for index, page in enumerate(document):
            if index not in pages_needing_ocr:
                combined_pages.append(pages[index])
                continue
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            combined_pages.append(pytesseract.image_to_string(image).strip())
        ocr_text = "\n\n".join(page for page in combined_pages if page)
        if len(ocr_text.strip()) < 10:
            raise DocumentParseError("No readable text was found. Check the scan quality and try again.")
        method = "embedded_text_and_ocr" if digital_text.strip() else "tesseract_ocr"
        return ocr_text.strip(), method
    except DocumentParseError:
        raise
    except Exception as exc:
        if digital_text.strip():
            return digital_text.strip(), "embedded_text_partial"
        if exc.__class__.__name__ == "TesseractNotFoundError":
            raise DocumentParseError(
                "This appears to be a scanned PDF. Install the Tesseract OCR application, "
                "then set TESSERACT_CMD if it is not on PATH."
            ) from exc
        raise DocumentParseError(
            "No embedded text was found and OCR could not run. Install Tesseract OCR and retry."
        ) from exc

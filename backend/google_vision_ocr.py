from __future__ import annotations

import base64
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


VISION_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"
MAX_OCR_PAGES = 40
MAX_RENDERED_IMAGE_BYTES = 12 * 1024 * 1024


class GoogleVisionError(RuntimeError):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def _google_vision_image_text(image_bytes: bytes, api_key: str) -> str:
    url = f"{VISION_ANNOTATE_URL}?{urlencode({'key': api_key})}"
    body = {
        "requests": [
            {
                "image": {"content": base64.b64encode(image_bytes).decode("ascii")},
                "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
            }
        ]
    }
    outbound = Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urlopen(outbound, timeout=60) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        try:
            response_data = json.loads(detail)
            detail = response_data.get("error", {}).get("message") or detail
        except (json.JSONDecodeError, AttributeError):
            pass
        raise GoogleVisionError(f"Google Vision request failed ({error.code}): {detail}") from error
    except (URLError, TimeoutError, OSError) as error:
        raise GoogleVisionError(f"Could not connect to Google Vision: {error}") from error
    except json.JSONDecodeError as error:
        raise GoogleVisionError("Google Vision returned invalid JSON.") from error

    responses = result.get("responses") or []
    if not responses:
        raise GoogleVisionError("Google Vision returned no page result.")
    page_result = responses[0]
    if page_result.get("error"):
        message = page_result["error"].get("message", "OCR failed for a PDF page.")
        raise GoogleVisionError(f"Google Vision OCR failed: {message}")
    return (page_result.get("fullTextAnnotation") or {}).get("text", "").strip()


def extract_assignment_pdf(pdf_bytes: bytes, api_key: str | None = None) -> dict:
    """Extract selectable PDF text and use Vision DOCUMENT_TEXT_DETECTION on scanned pages."""
    if not pdf_bytes.startswith(b"%PDF-"):
        raise GoogleVisionError("Upload a valid PDF file.", 415)

    try:
        import fitz
    except ImportError as error:
        raise GoogleVisionError("Install the PyMuPDF package to process PDF assignments.", 500) from error

    try:
        document = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as error:
        raise GoogleVisionError(f"Could not read this PDF: {error}", 422) from error

    if document.is_encrypted:
        raise GoogleVisionError("This PDF is password protected. Upload an unlocked PDF.", 422)
    if not document.page_count:
        raise GoogleVisionError("This PDF does not contain any pages.", 422)
    if document.page_count > MAX_OCR_PAGES:
        raise GoogleVisionError(f"PDFs may contain at most {MAX_OCR_PAGES} pages.", 413)

    key = (api_key or os.getenv("GOOGLE_VISION_API_KEY", "")).strip()
    extracted_pages = []
    used_vision = False
    used_embedded_text = False
    for page in document:
        embedded_text = (page.get_text("text") or "").strip()
        if len(embedded_text) >= 20:
            extracted_pages.append(embedded_text)
            used_embedded_text = True
            continue

        if not key:
            raise GoogleVisionError(
                "This PDF contains scanned pages. Add GOOGLE_VISION_API_KEY to the server .env, then retry.",
                503,
            )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image_bytes = pixmap.tobytes("png")
        if len(image_bytes) > MAX_RENDERED_IMAGE_BYTES:
            raise GoogleVisionError("A scanned page is too large for OCR. Reduce the PDF page resolution and retry.", 413)
        extracted_pages.append(_google_vision_image_text(image_bytes, key))
        used_vision = True

    text = "\n\n".join(page_text for page_text in extracted_pages if page_text).strip()
    if not text:
        raise GoogleVisionError("No readable text was found in the PDF.", 422)
    if used_vision and used_embedded_text:
        method = "embedded_text_and_google_vision"
    elif used_vision:
        method = "google_vision"
    else:
        method = "embedded_text"
    return {"text": text, "method": method, "page_count": document.page_count}

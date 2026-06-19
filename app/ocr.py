"""Optional OCR fallback for scanned PDF files.

All heavy imports (pytesseract, pdf2image, PIL) are done lazily inside the
function so that the module can always be imported, even when those packages
are not installed.  If any dependency is missing, or if the Tesseract binary
cannot be found, ``ocr_pdf_bytes`` returns an empty string and lets the caller
proceed normally — it never raises.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def ocr_pdf_bytes(file_bytes: bytes) -> str:
    """Convert a PDF to text via OCR.

    Parameters
    ----------
    file_bytes:
        Raw bytes of the PDF file.

    Returns
    -------
    str
        Extracted text (may be an empty string on any failure).
    """
    try:
        # Lazy imports – these are optional dependencies
        import pytesseract  # type: ignore[import]
        from pdf2image import convert_from_bytes  # type: ignore[import]
    except ImportError as exc:
        logger.debug('OCR skipped – optional package missing: %s', exc)
        return ''

    # Check that the Tesseract binary is actually available
    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError as exc:
        logger.debug('OCR skipped – Tesseract binary not found: %s', exc)
        return ''
    except Exception as exc:  # noqa: BLE001
        logger.debug('OCR skipped – unexpected error checking Tesseract: %s', exc)
        return ''

    try:
        images = convert_from_bytes(file_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning('OCR: pdf2image conversion failed: %s', exc)
        return ''

    pages: list[str] = []
    for i, image in enumerate(images):
        try:
            page_text: str = pytesseract.image_to_string(image)
            pages.append(page_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning('OCR: tesseract failed on page %d: %s', i + 1, exc)

    return '\n'.join(pages)

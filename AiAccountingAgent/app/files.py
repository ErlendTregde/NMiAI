"""
File attachment processing.

Strategy:
  PDF  → pypdf text extraction first; if sparse (<50 chars), fall back to Gemini vision
  Image → Gemini vision directly
  Other → UTF-8 decode

The extracted text is injected into the agent's initial user message so
Gemini can reference it while deciding which API calls to make.
"""
import base64
import logging
from typing import TYPE_CHECKING

from .schemas import SolveFile

if TYPE_CHECKING:
    from google import genai

logger = logging.getLogger(__name__)


def _extract_pdf_text(data: bytes) -> str:
    try:
        import io
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages).strip()
    except Exception as exc:
        logger.warning(f"pypdf extraction failed: {exc}")
        return ""


def _extract_via_gemini(
    data: bytes,
    filename: str,
    mime_type: str,
    client: "genai.Client",
    flash_model: str,
) -> str:
    """Use Gemini vision (flash model) to extract text from images or scanned PDFs."""
    try:
        from google.genai import types

        b64 = base64.b64encode(data).decode()
        response = client.models.generate_content(
            model=flash_model,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            inline_data=types.Blob(mime_type=mime_type, data=b64)
                        ),
                        types.Part(
                            text=(
                                f"Extract all text and data from this file ({filename}). "
                                "Return the raw content as plain text. "
                                "Include all numbers, dates, names, and amounts. "
                                "Be thorough and accurate."
                            )
                        ),
                    ],
                )
            ],
        )
        return response.text or ""
    except Exception as exc:
        logger.warning(f"Gemini vision extraction failed for {filename}: {exc}")
        return f"[Could not extract content from {filename}]"


def process_files(
    files: list[SolveFile],
    client: "genai.Client",
    flash_model: str,
) -> str:
    """
    Decode and extract text from all attached files.
    Returns a single string to be appended to the agent's user message.
    """
    if not files:
        return ""

    results: list[str] = []
    for f in files:
        try:
            data = base64.b64decode(f.content_base64)
        except Exception as exc:
            logger.warning(f"Failed to base64-decode {f.filename}: {exc}")
            results.append(f"[File: {f.filename}] — decode failed")
            continue

        logger.info(
            f"Processing attachment: {f.filename} ({f.mime_type}, {len(data)} bytes)"
        )

        if f.mime_type == "application/pdf":
            text = _extract_pdf_text(data)
            if len(text.strip()) < 50:
                logger.info(
                    f"PDF text sparse ({len(text.strip())} chars), "
                    f"falling back to Gemini vision for {f.filename}"
                )
                text = _extract_via_gemini(data, f.filename, f.mime_type, client, flash_model)
        elif f.mime_type.startswith("image/"):
            text = _extract_via_gemini(data, f.filename, f.mime_type, client, flash_model)
        else:
            try:
                text = data.decode("utf-8", errors="replace")
            except Exception:
                text = f"[Binary file: {f.filename}]"

        results.append(f"[File: {f.filename}]\n{text}")

    return "\n\n".join(results)

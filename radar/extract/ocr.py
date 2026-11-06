"""Optional OCR for poster images and screenshots.

Many Indian casting calls are images. OCR is enabled only when the user asks
for it; if Tesseract/RapidOCR are missing the pipeline continues without OCR.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from typing import Callable

from ..config import Settings
from ..util import get_logger
from .maintext import extract_images

log = get_logger(__name__)


@dataclass
class OcrResult:
    text: str = ""
    method: str = "none"
    images_used: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    available: bool = False


def ocr_available() -> dict[str, bool]:
    result = {"tesseract": False, "rapidocr": False, "pillow": False}
    try:
        import PIL  # noqa: F401

        result["pillow"] = True
    except ImportError:
        pass
    try:
        import pytesseract  # noqa: F401

        result["tesseract"] = True
    except ImportError:
        pass
    try:
        import rapidocr_onnxruntime  # noqa: F401

        result["rapidocr"] = True
    except ImportError:
        pass
    return result


def ocr_images(
    html: str,
    *,
    base_url: str | None,
    settings: Settings,
    fetch_bytes: Callable[[str], bytes | None] | None = None,
    max_images: int = 3,
    max_bytes: int = 5_000_000,
) -> OcrResult:
    result = OcrResult(available=any(ocr_available().values()))
    if not settings.enable_ocr:
        result.errors.append("OCR disabled (RADAR_ENABLE_OCR=false)")
        return result

    images = extract_images(html, base_url=base_url)
    # Prefer poster-like images.
    images.sort(key=lambda url: 0 if any(k in url.lower() for k in ("poster", "casting", "audition", "flyer")) else 1)
    for image_url in images[:max_images]:
        try:
            data = fetch_bytes(image_url) if fetch_bytes else _download(image_url, settings.effective_user_agent)
            if not data or len(data) > max_bytes:
                continue
            text, method = _ocr_bytes(data, settings)
            if text.strip():
                result.text += "\n" + text.strip()
                result.images_used.append(image_url)
                result.method = method
        except Exception as exc:
            result.errors.append(f"{image_url}: {exc}")
    result.text = result.text.strip()
    return result


def _download(url: str, user_agent: str) -> bytes | None:
    import httpx

    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": user_agent}) as client:
            response = client.get(url)
            if response.status_code == 200 and response.headers.get("content-type", "").startswith("image/"):
                return response.content
    except Exception as exc:
        log.debug("image download failed %s: %s", url, exc)
    return None


def _ocr_bytes(data: bytes, settings: Settings) -> tuple[str, str]:
    # Tesseract gives better Hindi results when hin+eng data is installed.
    try:
        import pytesseract
        from PIL import Image

        image = Image.open(BytesIO(data))
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        try:
            text = pytesseract.image_to_string(image, lang=settings.ocr_langs)
        except Exception:
            text = pytesseract.image_to_string(image)
        if text.strip():
            return text, "tesseract"
    except Exception as exc:
        log.debug("tesseract OCR failed: %s", exc)

    try:
        import numpy as np
        from PIL import Image
        from rapidocr_onnxruntime import RapidOCR

        image = Image.open(BytesIO(data)).convert("RGB")
        engine = RapidOCR()
        result, _ = engine(np.array(image))
        if result:
            text = "\n".join(str(item[1]) for item in result if len(item) > 1)
            return text, "rapidocr"
    except Exception as exc:
        log.debug("rapidocr failed: %s", exc)
    return "", "none"

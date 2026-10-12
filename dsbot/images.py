"""Image handling: download → shrink so the longest side is N pixels → convert to JPEG → data URI."""

from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger(__name__)

MAX_RAW_BYTES = 20 * 1024 * 1024  # originals bigger than this are skipped
MAX_PIXELS = 40_000_000  # check the dimensions before decoding and skip huge images, so they can't fill up memory
FALLBACK_MAX_BYTES = 4 * 1024 * 1024  # without Pillow, the size limit for sending an image as is

_SIGNATURES = [
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
]


def sniff_mime(raw: bytes) -> str | None:
    for sig, mime in _SIGNATURES:
        if raw.startswith(sig):
            return mime
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def to_data_uri(raw: bytes, max_side: int = 1024, quality: int = 80) -> str | None:
    if not raw or len(raw) > MAX_RAW_BYTES:
        return None
    try:
        from PIL import Image
    except ImportError:
        mime = sniff_mime(raw)
        if mime is None or len(raw) > FALLBACK_MAX_BYTES:
            return None
        return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")

    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width * img.height > MAX_PIXELS:
                log.info("Image too large (%dx%d), skipping", img.width, img.height)
                return None
            img.seek(0)  # only the first frame of animated images
            img.draft("RGB", (max_side * 2, max_side * 2))  # decode JPEGs straight at a reduced size to save memory
            frame = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
            if frame.mode == "RGBA":
                background = Image.new("RGB", frame.size, (255, 255, 255))
                background.paste(frame, mask=frame.split()[3])
                frame = background
            frame.thumbnail((max_side, max_side))
            out = io.BytesIO()
            frame.save(out, format="JPEG", quality=quality, optimize=True)
    except Exception as exc:
        log.info("Image processing failed, skipping: %r", exc)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")

"""Image handling: download → shrink so the longest side is N pixels → convert to JPEG → data URI."""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

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


def pixel_count(raw: bytes) -> int | None:
    """How many pixels the image has (reads only the header, no decoding). Returns None without Pillow or when it can't be opened."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            return img.width * img.height
    except Exception:
        return None


_ALPHA_MODES = ("RGBA", "LA", "PA", "RGBa", "La")
_DEEP_MODES = ("I", "I;16", "I;16L", "I;16B", "I;16N")  # 16-bit (occasionally 32-bit) grayscale images


def _maybe_transparent(img: Any) -> bool:
    """Header only: there may be transparent parts (an alpha channel, or a PNG / GIF that marks one color as transparent)."""
    return img.mode in _ALPHA_MODES or "transparency" in img.info


def _to_rgb(img: Any) -> tuple[Any, bool]:
    """Convert to RGB and return (image, whether it looks different from the original).
    Transparent areas get a white background (in a dark theme, things like white text on a transparent background would otherwise look
    different from what the model saw); images with an alpha channel that are actually fully opaque (screenshots are often saved that way)
    are converted as is. 16-bit grayscale is scaled down to 8 bits first: converting straight to RGB would clip every value above 255 to white."""
    from PIL import Image

    if img.mode in _DEEP_MODES:
        gray = img.convert("I").point(lambda v: v * (1 / 256))
        gray.info.pop("transparency", None)  # its "transparent color" is a 16-bit value that no longer matches after scaling to 8 bits: treat it as opaque
        return gray.convert("L").convert("RGB"), "transparency" in img.info
    if not _maybe_transparent(img):
        return img.convert("RGB"), False
    rgba = img.convert("RGBA")
    alpha = rgba.getchannel("A")
    if alpha.getextrema()[0] == 255:
        return rgba.convert("RGB"), False
    background = Image.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=alpha)
    return background, True


def upload_ready(raw: bytes, max_bytes: int, *, max_pixels: int = MAX_PIXELS) -> tuple[bytes, str] | None:
    """An image to send → (content, type). Usually sent as is; transparent ones get a white background (the same as the downsized
    version the model saw, so a dark theme doesn't show anything the model didn't see) and oversized ones get shrunk, and both of
    those are saved as JPEG. Returns None when that can't be done."""
    mime = sniff_mime(raw)
    fits = mime is not None and len(raw) <= max_bytes
    try:
        from PIL import Image
    except ImportError:
        return (raw, mime) if fits and mime is not None else None
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.width * img.height > max_pixels:
                return None
            if fits and mime is not None and img.mode not in _DEEP_MODES and not _maybe_transparent(img):
                return raw, mime  # the most common case: no decoding needed
            img.seek(0)
            frame, changed = _to_rgb(img)
    except Exception as exc:
        log.info("Preparing an image to send failed: %r", exc)
        return None
    if fits and mime is not None and not changed:
        return raw, mime
    for side in (None, 2048, 1280):
        try:
            candidate = frame
            if side is not None:
                if max(frame.size) <= side:
                    continue
                candidate = frame.copy()
                candidate.thumbnail((side, side))
            out = io.BytesIO()
            candidate.save(out, format="JPEG", quality=90)
        except Exception as exc:  # e.g. very long strip images (JPEG allows at most 65500 pixels across): they can still be saved once shrunk
            log.info("Saving an image to send as JPEG failed (%s): %r", f"shrunk to {side}" if side else "original size", exc)
            continue
        if out.tell() <= max_bytes:
            return out.getvalue(), "image/jpeg"
    return None


def filename_for(mime: str, stem: str = "image") -> str:
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(mime, "png")
    return f"{stem}.{ext}"


def is_animated(raw: bytes) -> bool:
    """Whether it's animated (a multi-frame GIF / WebP / PNG). When unsure (no Pillow, or it can't be opened), it counts as animated."""
    try:
        from PIL import Image
    except ImportError:
        return True
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format == "MPO":  # several JPEGs stored together (stereo photos, HDR gain maps): only the first one is looked at, so it's not animated
                return False
            return bool(getattr(img, "is_animated", False)) or getattr(img, "n_frames", 1) > 1
    except Exception:
        return True


def to_data_uri(raw: bytes, max_side: int = 1024, quality: int = 80, *, max_pixels: int = MAX_PIXELS) -> str | None:
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
            if img.width * img.height > max_pixels:
                log.info("Image too large (%dx%d), skipping", img.width, img.height)
                return None
            img.seek(0)  # only the first frame of animated images
            img.draft("RGB", (max_side * 2, max_side * 2))  # decode JPEGs straight at a reduced size to save memory
            frame, _ = _to_rgb(img)
            frame.thumbnail((max_side, max_side))
            out = io.BytesIO()
            frame.save(out, format="JPEG", quality=quality, optimize=True)
    except Exception as exc:
        log.info("Image processing failed, skipping: %r", exc)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")

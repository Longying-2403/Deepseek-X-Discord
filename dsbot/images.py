"""图片处理：下载 → 缩到最长边 N 像素 → 转 JPEG → data URI。"""

from __future__ import annotations

import base64
import io
import logging

log = logging.getLogger(__name__)

MAX_RAW_BYTES = 20 * 1024 * 1024  # 超过这个大小的原图直接跳过
MAX_PIXELS = 40_000_000  # 解码前先看尺寸，超大图跳过，防止占满内存
FALLBACK_MAX_BYTES = 4 * 1024 * 1024  # 没装 Pillow 时，原样发送的上限

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
                log.info("图片太大（%dx%d），跳过", img.width, img.height)
                return None
            img.seek(0)  # 动图只取第一帧
            img.draft("RGB", (max_side * 2, max_side * 2))  # JPEG 直接按缩小的尺寸解码，省内存
            frame = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
            if frame.mode == "RGBA":
                background = Image.new("RGB", frame.size, (255, 255, 255))
                background.paste(frame, mask=frame.split()[3])
                frame = background
            frame.thumbnail((max_side, max_side))
            out = io.BytesIO()
            frame.save(out, format="JPEG", quality=quality, optimize=True)
    except Exception as exc:
        log.info("图片处理失败，跳过：%r", exc)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")

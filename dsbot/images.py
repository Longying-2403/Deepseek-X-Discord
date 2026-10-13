"""图片处理：下载 → 缩到最长边 N 像素 → 转 JPEG → data URI。"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

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


def pixel_count(raw: bytes) -> int | None:
    """图有多少像素（只读文件头，不解码）。没装 Pillow、打不开时返回 None。"""
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
_DEEP_MODES = ("I", "I;16", "I;16L", "I;16B", "I;16N")  # 16 位（偶尔 32 位）灰度图


def _maybe_transparent(img: Any) -> bool:
    """只看文件头：可能有透明的部分（带 alpha 通道，或者 PNG / GIF 指定了某个颜色透明）。"""
    return img.mode in _ALPHA_MODES or "transparency" in img.info


def _to_rgb(img: Any) -> tuple[Any, bool]:
    """转成 RGB，返回 (图, 和原图看起来是否不一样)。
    透明的地方铺白底（深色主题里，透明底上的白字之类就会是模型没看到的样子）；带 alpha 通道、其实全不透明的
    （截图常这样存）原样转。16 位灰度图先缩成 8 位：直接转 RGB 会把超过 255 的值全截成白色。"""
    from PIL import Image

    if img.mode in _DEEP_MODES:
        gray = img.convert("I").point(lambda v: v * (1 / 256))
        gray.info.pop("transparency", None)  # 它的“透明色”是 16 位的值，缩成 8 位后对不上了：当不透明处理
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
    """要发出去的图 → (内容, 类型)。一般原样发；透明的铺上白底（和给模型看的缩小版一样，免得深色主题里多出
    模型没看到的东西），太大的缩小，这两种存成 JPEG。做不到就返回 None。"""
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
                return raw, mime  # 最常见的情况：不用解码
            img.seek(0)
            frame, changed = _to_rgb(img)
    except Exception as exc:
        log.info("准备要发的图失败：%r", exc)
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
        except Exception as exc:  # 比如超长条的图（JPEG 最多 65500 像素宽）：缩小了还能存
            log.info("要发的图存成 JPEG 失败（%s）：%r", f"缩到 {side}" if side else "原尺寸", exc)
            continue
        if out.tell() <= max_bytes:
            return out.getvalue(), "image/jpeg"
    return None


def filename_for(mime: str, stem: str = "image") -> str:
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}.get(mime, "png")
    return f"{stem}.{ext}"


def is_animated(raw: bytes) -> bool:
    """是不是动图（多帧的 GIF / WebP / PNG）。拿不准（没装 Pillow、打不开）时按动图算。"""
    try:
        from PIL import Image
    except ImportError:
        return True
    try:
        with Image.open(io.BytesIO(raw)) as img:
            if img.format == "MPO":  # 几张 JPEG 存在一起（立体照片、HDR 增益图），看的是第一张，不是动图
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
                log.info("图片太大（%dx%d），跳过", img.width, img.height)
                return None
            img.seek(0)  # 动图只取第一帧
            img.draft("RGB", (max_side * 2, max_side * 2))  # JPEG 直接按缩小的尺寸解码，省内存
            frame, _ = _to_rgb(img)
            frame.thumbnail((max_side, max_side))
            out = io.BytesIO()
            frame.save(out, format="JPEG", quality=quality, optimize=True)
    except Exception as exc:
        log.info("图片处理失败，跳过：%r", exc)
        return None
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")

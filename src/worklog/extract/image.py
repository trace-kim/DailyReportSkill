"""Image metadata. Pixel content is left for a multimodal model later."""

from __future__ import annotations

from pathlib import Path

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic"}


def image_meta(path: Path) -> dict:
    meta: dict = {}
    try:
        from PIL import Image

        with Image.open(path) as im:
            meta.update(width=im.width, height=im.height, format=im.format, mode=im.mode)
            try:
                exif = im.getexif()
                taken = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
                if taken:
                    meta["taken"] = str(taken)
            except Exception:
                pass
    except Exception as e:
        meta["error"] = f"{type(e).__name__}: {e}"
    return meta

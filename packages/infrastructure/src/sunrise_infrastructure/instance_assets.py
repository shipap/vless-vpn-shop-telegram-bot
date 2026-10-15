"""Выдача только проверенных файлов конкретных ролей экземпляра."""

import io
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image

from . import instance_config

MAX_ASSET_BYTES = 2_000_000
MIMES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}
SVG_ELEMENTS = {
    "svg",
    "g",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "text",
    "tspan",
    "title",
    "desc",
    "defs",
    "linearGradient",
    "radialGradient",
    "stop",
    "clipPath",
    "mask",
}
SVG_ATTRIBUTES = {
    "id",
    "viewBox",
    "width",
    "height",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "cx",
    "cy",
    "r",
    "rx",
    "ry",
    "d",
    "points",
    "fill",
    "stroke",
    "stroke-width",
    "stroke-linecap",
    "stroke-linejoin",
    "fill-rule",
    "clip-rule",
    "opacity",
    "fill-opacity",
    "stroke-opacity",
    "transform",
    "font-size",
    "font-family",
    "font-weight",
    "text-anchor",
    "offset",
    "stop-color",
    "stop-opacity",
    "gradientUnits",
    "gradientTransform",
    "clip-path",
    "mask",
}


def reparse(path):
    try:
        return (
            path.is_symlink()
            or path.is_junction()
            or bool(getattr(path.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        )
    except (OSError, ValueError):
        return False


def _safe_svg(content):
    try:
        text = content.decode("utf-8", errors="strict")
        if "\x00" in text or re.search(r"<!DOCTYPE|<!ENTITY|<\?", text, re.I):
            return False
        root = ET.fromstring(text)  # noqa: S314 -- UTF-8 only; DTD/entities/PI rejected above; bounded to 2MB
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            return False
        nodes = list(root.iter())
        if len(nodes) > 2000:
            return False
        for node in nodes:
            if (
                not node.tag.startswith("{http://www.w3.org/2000/svg}")
                or node.tag.split("}")[-1] not in SVG_ELEMENTS
            ):
                return False
            for key, value in node.attrib.items():
                if (
                    key not in SVG_ATTRIBUTES
                    or "\\" in value
                    or any(ord(character) < 32 for character in value)
                    or re.search(r"[<>]|://|data:|javascript:", value, re.I)
                ):
                    return False
                if "url" in value.lower() and not re.fullmatch(r"url\(#[A-Za-z][A-Za-z0-9_-]{0,80}\)", value):
                    return False
        return True
    except (ET.ParseError, ValueError):
        return False


def read_asset(config, role, *, project_root=None):
    field = instance_config.ASSET_ROLES.get(role)
    if not field or not (relative := getattr(config, field)):
        return None
    root = Path(project_root or instance_config.PROJECT_ROOT).resolve()
    asset_root = root / config.asset_root
    path = asset_root / relative
    try:
        for component in [path, *path.parents]:
            if component == root:
                break
            if reparse(component):
                return None
        if (
            not path.resolve(strict=True).is_relative_to(asset_root.resolve(strict=True))
            or not asset_root.resolve().is_relative_to(root)
            or not path.is_file()
            or path.suffix.lower() not in MIMES
            or path.suffix.lower() == ".ico"
            and role != "favicon"
            or path.suffix.lower() == ".svg"
            and role not in {"logo", "favicon"}
        ):
            return None
        with path.open("rb") as stream:
            content = stream.read(MAX_ASSET_BYTES + 1)
        if not content or len(content) > MAX_ASSET_BYTES:
            return None
        suffix = path.suffix.lower()
        if suffix == ".svg":
            if not _safe_svg(content):
                return None
        else:
            with Image.open(io.BytesIO(content)) as image:
                if (
                    image.width * image.height > 16_000_000
                    or image.format
                    != {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".ico": "ICO"}[
                        suffix
                    ]
                ):
                    return None
                image.verify()
        return content, MIMES[suffix]
    except (OSError, ValueError, Image.DecompressionBombError):
        return None

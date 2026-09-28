"""Team colour badges for the player finder.

Colours come from the game's own team records; badges are drawn with Pillow at
4x and downsampled so the edges stay smooth at list size.
"""
from __future__ import annotations

import colorsys
import hashlib

from PIL import Image, ImageDraw, ImageFont, ImageTk

SCALE = 4
LATIN_FONTS = ("bahnschrift.ttf", "segoeuib.ttf", "arialbd.ttf")
CJK_FONTS = ("msyhbd.ttc", "msyh.ttc", "simhei.ttf")

LEAGUE_STYLE = {
    "NBA": ("NBA", "#1d428a", "#c8102e"),
    "WNBA": ("WNBA", "#fa4d00", "#f5f5f5"),
    "G 联盟": ("G", "#c8102e", "#f5f5f5"),
    "国家队": ("国家队", "#b8912f", "#f5f5f5"),
    "其他联赛": ("其他", "#475063", "#9aa3b2"),
    "自由球员": ("FA", "#343a46", "#8b93a1"),
}


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c))) for c in rgb[:3])


def mix(a: str, b: str, amount: float) -> str:
    """Blend colour a towards b by amount (0 = a, 1 = b)."""
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex([x + (y - x) * amount for x, y in zip(ra, rb)])


def luminance(color: str) -> float:
    def channel(c):
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in hex_to_rgb(color))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def text_on(color: str) -> str:
    return "#111418" if luminance(color) > 0.42 else "#ffffff"


def fallback_colors(key: str) -> tuple[str, str]:
    """Stable, readable colours for teams whose record has no usable colours."""
    digest = hashlib.md5(key.encode("utf-8")).digest()
    hue = digest[0] / 255
    primary = colorsys.hls_to_rgb(hue, 0.42, 0.55)
    secondary = colorsys.hls_to_rgb((hue + 0.5) % 1, 0.62, 0.45)
    return rgb_to_hex([c * 255 for c in primary]), rgb_to_hex([c * 255 for c in secondary])


def colors_from_record(raw: bytes) -> tuple[str, str] | None:
    """Decode the two RGBA team colours (primary, secondary) read at team+4940."""
    if len(raw) < 8 or raw[3] != 0xFF or raw[7] != 0xFF:
        return None
    primary, secondary = rgb_to_hex(raw[0:3]), rgb_to_hex(raw[4:7])
    if primary == secondary:
        secondary = mix(primary, "#ffffff", 0.55)
    if luminance(secondary) < 0.08:
        # A dark ring around a dark badge disappears on the dark theme.
        secondary = mix(secondary, "#ffffff", 0.4)
    return primary, secondary


def tier_color(overall: int | None) -> str:
    if overall is None:
        return "#6b7280"
    if overall >= 90:
        return "#ffc53d"
    if overall >= 80:
        return "#46d17d"
    if overall >= 70:
        return "#4c9aff"
    if overall >= 60:
        return "#9aa3b2"
    return "#6b7280"


def _font(text: str, size: int) -> ImageFont.FreeTypeFont:
    names = LATIN_FONTS if text.isascii() else CJK_FONTS
    for name in names:
        try:
            font = ImageFont.truetype(name, size)
        except OSError:
            continue
        if name.startswith("bahnschrift"):
            try:
                font.set_variation_by_name("SemiBold")
            except Exception:
                pass
        return font
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int, start: int):
    size = start
    while size > 8:
        font = _font(text, size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        if right - left <= box_w and bottom - top <= box_h:
            return font, (left, top, right, bottom)
        size -= 2
    font = _font(text, size)
    return font, draw.textbbox((0, 0), text, font=font)


def _centered_text(draw, text, frame, color, start_size):
    x0, y0, x1, y1 = frame
    font, (left, top, right, bottom) = _fit_font(draw, text, x1 - x0, y1 - y0, start_size)
    x = x0 + (x1 - x0 - (right - left)) / 2 - left
    y = y0 + (y1 - y0 - (bottom - top)) / 2 - top
    draw.text((x, y), text, font=font, fill=color)


class BadgeFactory:
    """Creates and caches Tk images; Tk needs the references kept alive."""

    def __init__(self, master):
        self.master = master
        self._cache: dict[tuple, ImageTk.PhotoImage] = {}

    def _finish(self, key, image: Image.Image):
        width, height = image.size
        photo = ImageTk.PhotoImage(
            image.resize((width // SCALE, height // SCALE), Image.Resampling.LANCZOS), master=self.master)
        self._cache[key] = photo
        return photo

    def pill(self, text: str, primary: str, secondary: str, *, width: int = 44, height: int = 20):
        key = ("pill", text, primary, secondary, width, height)
        if key in self._cache:
            return self._cache[key]
        w, h, ring = width * SCALE, height * SCALE, 2 * SCALE
        image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=secondary)
        draw.rounded_rectangle((ring, ring, w - 1 - ring, h - 1 - ring), radius=h // 2 - ring, fill=primary)
        _centered_text(draw, text, (ring * 3, ring * 2, w - ring * 3, h - ring * 2), text_on(primary), 13 * SCALE)
        return self._finish(key, image)

    def dot(self, primary: str, secondary: str, *, size: int = 12):
        key = ("dot", primary, secondary, size)
        if key in self._cache:
            return self._cache[key]
        s, ring = (size + 6) * SCALE, 2 * SCALE
        image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        pad = 3 * SCALE
        draw.ellipse((pad, pad, s - pad, s - pad), fill=secondary)
        draw.ellipse((pad + ring, pad + ring, s - pad - ring, s - pad - ring), fill=primary)
        return self._finish(key, image)

    def disc(self, text: str, primary: str, secondary: str, *, size: int = 54):
        key = ("disc", text, primary, secondary, size)
        if key in self._cache:
            return self._cache[key]
        s, ring = size * SCALE, 3 * SCALE
        image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((0, 0, s - 1, s - 1), fill=secondary)
        draw.ellipse((ring, ring, s - 1 - ring, s - 1 - ring), fill=primary)
        inset = s // 5
        _centered_text(draw, text, (inset, inset, s - inset, s - inset), text_on(primary), 20 * SCALE)
        return self._finish(key, image)

    def chip(self, text: str, color: str, *, width: int = 58, height: int = 34):
        """Rounded rating chip used in the player header."""
        key = ("chip", text, color, width, height)
        if key in self._cache:
            return self._cache[key]
        w, h = width * SCALE, height * SCALE
        image = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=8 * SCALE, fill=color)
        _centered_text(draw, text, (6 * SCALE, 4 * SCALE, w - 6 * SCALE, h - 4 * SCALE), text_on(color), 24 * SCALE)
        return self._finish(key, image)

    def league(self, league: str):
        text, primary, secondary = LEAGUE_STYLE.get(league, (league[:3], "#475063", "#9aa3b2"))
        return self.pill(text, primary, secondary, width=max(44, 14 + 10 * len(text)))

    def glyph(self, name: str, color: str, size: int = 16):
        """Segoe Fluent/MDL2 icon rendered as an image (ttk fonts cannot mix glyphs)."""
        key = ("glyph", name, color, size)
        if key in self._cache:
            return self._cache[key]
        s = (size + 2) * SCALE
        image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        char = GLYPHS.get(name, "\uE783")
        for font_name in ICON_FONTS:
            try:
                font = ImageFont.truetype(font_name, size * SCALE)
                break
            except OSError:
                font = None
        if font is None:
            draw.ellipse((s // 4, s // 4, s * 3 // 4, s * 3 // 4), fill=color)
        else:
            left, top, right, bottom = draw.textbbox((0, 0), char, font=font)
            draw.text(((s - (right - left)) / 2 - left, (s - (bottom - top)) / 2 - top), char, font=font, fill=color)
        return self._finish(key, image)


ICON_FONTS = ("SegoeIcons.ttf", "segmdl2.ttf")
GLYPHS = {
    "search": "\uE721", "clear": "\uE711", "refresh": "\uE72C", "save": "\uE74E", "undo": "\uE7A7",
    "copy": "\uE8C8", "edit": "\uE70F", "link": "\uE71B", "camera": "\uE722", "scan": "\uE71E",
    "warning": "\uE7BA", "sync": "\uE895", "people": "\uE716", "check": "\uE73E", "stop": "\uE71A",
    "add": "\uE710", "delete": "\uE74D", "reload": "\uE72C", "target": "\uE7B7",
}

"""Draws the branded 1080x1350 post graphic with Pillow (no third-party photos)."""
import datetime as dt
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import ROOT

W, H = 1080, 1350
MARGIN = 84
TOP_PAD = 0       # extra space at the top/bottom, used for the vertical Reel format
BOTTOM_PAD = 0
REEL = False      # True while drawing Reel frames (no "Swipe" hints in a video)
FONTS = ROOT / "assets" / "fonts"


from contextlib import contextmanager


@contextmanager
def reel_format():
    """Temporarily switch the layout to 1080x1920, keeping text inside Instagram's Reels safe zone."""
    global W, H, TOP_PAD, BOTTOM_PAD, REEL
    old = (W, H, TOP_PAD, BOTTOM_PAD, REEL)
    W, H, TOP_PAD, BOTTOM_PAD, REEL = 1080, 1920, 190, 330, True
    try:
        yield
    finally:
        W, H, TOP_PAD, BOTTOM_PAD, REEL = old


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for n in names:
        p = FONTS / n
        if p.exists():
            return ImageFont.truetype(str(p), size)
    return ImageFont.load_default(size=size)


def headline_font(size):
    # Anton is downloaded by the workflow if available; DejaVu is bundled as a fallback.
    return _font(["Anton-Regular.ttf", "DejaVuSansCondensed-Bold.ttf"], size)


def bold_font(size):
    return _font(["DejaVuSansCondensed-Bold.ttf", "DejaVuSans-Bold.ttf"], size)


def regular_font(size):
    return _font(["DejaVuSans.ttf"], size)


def _hex(c: str, alpha: int = 255):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) + (alpha,)


def _wrap(draw, text, font, max_width):
    words, lines, line = text.split(), [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if draw.textlength(test, font=font) <= max_width:
            line = test
        else:
            if line:
                lines.append(line)
            line = w
    if line:
        lines.append(line)
    return lines


def _fit_headline(draw, text, max_width, max_height):
    uses_anton = (FONTS / "Anton-Regular.ttf").exists()
    if uses_anton:
        text = text.upper()
    for size in range(112, 50, -4):
        font = headline_font(size)
        lines = _wrap(draw, text, font, max_width)
        line_h = int(size * (1.08 if uses_anton else 1.15))
        if len(lines) * line_h <= max_height and all(draw.textlength(l, font=font) <= max_width for l in lines):
            return font, lines, line_h
    font = headline_font(52)
    lines = _wrap(draw, text, font, max_width)[:7]
    return font, lines, 60


def _base(settings: dict, deco_wheel: bool = True, handle: str = "", background=None, bg_style: str = "cover"):
    """Background, gradient, optional decoration, top bar and logo. Returns (img, draw, colors, logo)."""
    d = settings["design"]
    colors = {k: _hex(d[k]) for k in ("background", "accent", "text", "muted")}
    bg, accent, fg = colors["background"], colors["accent"], colors["text"]

    img = Image.new("RGBA", (W, H), bg)

    logo = None
    logo_path = ROOT / d.get("logo", "")
    if d.get("logo") and logo_path.is_file():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((300, 150))
        except Exception:
            logo = None

    # Optional photo background (e.g. your own game screenshots), darkened so text stays readable.
    if background:
        try:
            img = _photo_background(background, colors["background"], bg_style)
        except Exception as ex:
            print(f"[bot] Couldn't use background image {background}: {ex}", flush=True)
            background = None

    # Soft vertical gradient for depth.
    grad = Image.new("RGBA", (W, H))
    gd = ImageDraw.Draw(grad)
    for y in range(H):
        gd.line([(0, y), (W, y)], fill=(0, 0, 0, int(70 * (y / H))))
    img = Image.alpha_composite(img, grad)

    # Decorative "wheel": a faint road wheel (tyre, deep aero rim, bladed spokes, hub, disc rotor) top-right.
    if deco_wheel and not background and d.get("decoration", "wheel") == "wheel":
        img = Image.alpha_composite(img, _wheel(accent))

    draw = ImageDraw.Draw(img)
    # Top left: the logo, or the page name if there's no logo file.
    if logo is not None:
        img.alpha_composite(logo, (MARGIN, TOP_PAD + MARGIN - 10))
    else:
        draw.rectangle([MARGIN, TOP_PAD + MARGIN, MARGIN + 64, TOP_PAD + MARGIN + 10], fill=accent)
        draw.text((MARGIN, TOP_PAD + MARGIN + 30), settings["page"]["name"].upper(), font=bold_font(34), fill=fg)
    # Top right: the @handle.
    if handle:
        hf = regular_font(30)
        draw.text((W - MARGIN - draw.textlength(handle, font=hf), TOP_PAD + MARGIN + 6), handle, font=hf,
                  fill=fg if background else colors["muted"])   # white over photos for contrast
    return img, draw, colors, logo


def _photo_background(path, bg_color, style):
    from PIL import ImageFilter
    photo = Image.open(path).convert("RGB")
    # scale to cover the whole canvas, then crop the middle
    scale = max(W / photo.width, H / photo.height)
    photo = photo.resize((int(photo.width * scale) + 1, int(photo.height * scale) + 1), Image.LANCZOS)
    left, top = (photo.width - W) // 2, (photo.height - H) // 2
    photo = photo.crop((left, top, left + W, top + H)).convert("RGBA")
    if style == "slide":
        photo = photo.filter(ImageFilter.GaussianBlur(14))
    overlay = Image.new("RGBA", (W, H))
    od = ImageDraw.Draw(overlay)
    for y in range(H):
        if style == "slide":
            a = 0.84
        else:   # cover: lighter at the top, darker where the headline sits
            a = 0.25 + 0.70 * (y / H) ** 0.8
        od.line([(0, y), (W, y)], fill=bg_color[:3] + (int(255 * a),))
    return Image.alpha_composite(photo, overlay)


def _wheel(accent):
    import math
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = W - 120, TOP_PAD + 250
    a = lambda alpha: accent[:3] + (alpha,)

    def ring(r, width, alpha):
        d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=a(alpha), width=width)

    ring(332, 18, 42)          # tyre
    ring(308, 72, 26)          # deep-section aero rim (about 70 px deep)
    ring(308, 3, 40)           # rim outer edge (brake track)
    ring(296, 2, 30)
    ring(237, 3, 40)           # rim inner edge
    # 24 bladed spokes in a crossed pattern, from the hub flange to the deep rim
    for k in range(24):
        ang = 2 * math.pi * k / 24
        lean = 0.2 if k % 2 else -0.2
        x1, y1 = cx + 30 * math.cos(ang + lean * 4), cy + 30 * math.sin(ang + lean * 4)
        x2, y2 = cx + 236 * math.cos(ang), cy + 236 * math.sin(ang)
        d.line([(x1, y1), (x2, y2)], fill=a(24), width=3)
    # disc brake rotor: braking band with cut-outs, inner carrier and spider arms
    ring(86, 18, 34)
    for k in range(18):
        ang = 2 * math.pi * k / 18
        px, py = cx + 86 * math.cos(ang), cy + 86 * math.sin(ang)
        d.ellipse([px - 3.5, py - 3.5, px + 3.5, py + 3.5], fill=(0, 0, 0, 0))   # drilled holes
    ring(64, 3, 30)
    for k in range(6):
        ang = 2 * math.pi * k / 6 + 0.26
        d.line([(cx + 30 * math.cos(ang), cy + 30 * math.sin(ang)),
                (cx + 64 * math.cos(ang + 0.18), cy + 64 * math.sin(ang + 0.18))], fill=a(34), width=8)
    # hub body + axle
    d.ellipse([cx - 30, cy - 30, cx + 30, cy + 30], fill=a(30), outline=a(44), width=4)
    d.ellipse([cx - 11, cy - 11, cx + 11, cy + 11], fill=a(50))
    return layer


def _footer(draw, colors, source, handle, logo, right_text=None):
    footer_top = H - MARGIN - 90 - BOTTOM_PAD
    max_w = W - 2 * MARGIN
    draw.rectangle([MARGIN, footer_top, W - MARGIN, footer_top + 4], fill=colors["accent"][:3] + (200,))
    small = regular_font(30)
    if right_text is None:
        right_text = dt.date.today().strftime("%d %b %Y")
    right_font = bold_font(30) if right_text.startswith("Swipe") else small
    right_color = colors["accent"] if right_text.startswith("Swipe") else colors["muted"]
    rw = draw.textlength(right_text, font=right_font)
    src_txt = f"Source: {source}"
    while draw.textlength(src_txt, font=small) > max_w - rw - 40 and len(src_txt) > 12:
        src_txt = src_txt[:-2].rstrip() + "…" if not src_txt.endswith("…") else src_txt[:-2] + "…"
    draw.text((MARGIN, footer_top + 30), src_txt, font=small, fill=colors["muted"])
    draw.text((W - MARGIN - rw, footer_top + 30), right_text, font=right_font, fill=right_color)
    return footer_top


def _save(img, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


def render(headline: str, category: str, source: str, settings: dict, handle: str, out_path: Path,
           swipe: bool = False, background=None) -> Path:
    """The cover / single-image post: category tag + big headline."""
    img, draw, colors, logo = _base(settings, handle=handle, background=background, bg_style="cover")
    bg, accent, fg = colors["background"], colors["accent"], colors["text"]
    footer_top = _footer(draw, colors, source, handle, logo, right_text="Swipe for the story  →" if swipe else None)

    max_w = W - 2 * MARGIN
    font, lines, line_h = _fit_headline(draw, headline, max_w, 620)
    y = footer_top - 70 - len(lines) * line_h

    tag_font = bold_font(32)
    tag = category.upper()
    tw = draw.textlength(tag, font=tag_font)
    tag_y = y - 100
    draw.rounded_rectangle([MARGIN, tag_y, MARGIN + tw + 44, tag_y + 60], radius=8, fill=accent)
    draw.text((MARGIN + 22, tag_y + 10), tag, font=tag_font, fill=bg)

    for line in lines:
        draw.text((MARGIN, y), line, font=font, fill=fg)
        y += line_h
    return _save(img, out_path)


def render_slide(number: int, total: int, title: str, text: str, source: str, settings: dict,
                 handle: str, out_path: Path, background=None) -> Path:
    """A story slide in a carousel: slide counter, short title and body text."""
    img, draw, colors, logo = _base(settings, deco_wheel=False, handle=handle, background=background, bg_style="slide")
    accent, fg = colors["accent"], colors["text"]
    last = number == total
    footer_top = _footer(draw, colors, source, handle, logo,
                         right_text=None if (last or REEL) else "Swipe  →")

    max_w = W - 2 * MARGIN
    top = TOP_PAD + MARGIN + 200

    # Title.
    y = top + 40
    tf_size = 72
    while tf_size > 40:
        tf = bold_font(tf_size)
        t_lines = _wrap(draw, title, tf, max_w)
        if len(t_lines) <= 3:
            break
        tf_size -= 4
    for line in t_lines:
        draw.text((MARGIN, y), line, font=tf, fill=fg)
        y += int(tf_size * 1.18)
    y += 36
    draw.rectangle([MARGIN, y, MARGIN + 90, y + 8], fill=accent)
    y += 56

    # Body text, shrunk until it fits above the footer.
    room = footer_top - (130 if last else 50) - y
    for size in range(48, 28, -2):
        bf = regular_font(size)
        b_lines = []
        for para in text.split("\n"):
            b_lines += _wrap(draw, para, bf, max_w) or [""]
        lh = int(size * 1.42)
        if len(b_lines) * lh <= room:
            break
    for line in b_lines:
        draw.text((MARGIN, y), line, font=bf, fill=colors["text"][:3] + (235,))
        y += lh

    # Last slide: a follow prompt just above the footer, if there's room.
    if last and handle:
        cta = f"Follow {handle} {settings['page'].get('tagline', 'for daily news')}"
        cf2 = bold_font(36)
        if draw.textlength(cta, font=cf2) > max_w:
            cta = f"Follow {handle}"
        if y < footer_top - 110:
            draw.text((MARGIN, footer_top - 80), cta, font=cf2, fill=accent)
    return _save(img, out_path)

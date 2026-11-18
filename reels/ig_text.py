"""Instagram-style caption overlays rendered with Pillow.

White bold sentence-case text with a soft drop shadow and real color emoji,
centered, like the native Instagram Reels text. Each overlay is a transparent
1080x1920 PNG that ffmpeg lays over the video.
"""
from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
TEXT_FONT = "/usr/share/fonts/opentype/inter/Inter-Bold.otf"
EMOJI_FONT = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
EMOJI_NATIVE = 109  # Noto Color Emoji only has bitmaps at this size
MAX_TEXT_W = 900


def _is_emoji_char(ch):
    o = ord(ch)
    return (o >= 0x1F000 or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF
            or o in (0x200D, 0xFE0F, 0x20E3, 0x2122, 0x00A9, 0x00AE, 0x231A, 0x231B, 0x23F0))


def _runs(text):
    """Split text into [(is_emoji, string)] runs."""
    runs = []
    for ch in text:
        e = _is_emoji_char(ch)
        if runs and runs[-1][0] == e:
            runs[-1] = (e, runs[-1][1] + ch)
        else:
            runs.append((e, ch))
    return runs


def _emoji_clusters(s):
    """Group an emoji run into individual emoji (keeping ZWJ / VS16 / flags together)."""
    out = []
    for ch in s:
        o = ord(ch)
        joiner = o in (0x200D, 0xFE0F, 0x20E3) or 0x1F3FB <= o <= 0x1F3FF
        if out and (joiner or out[-1].endswith("‍")
                    or (0x1F1E6 <= o <= 0x1F1FF and len(out[-1]) == 1 and 0x1F1E6 <= ord(out[-1]) <= 0x1F1FF)):
            out[-1] += ch
        else:
            out.append(ch)
    return out


class _Renderer:
    def __init__(self, size):
        self.size = size
        self.font = ImageFont.truetype(TEXT_FONT, size)
        self.efont = ImageFont.truetype(EMOJI_FONT, EMOJI_NATIVE)
        self.esize = int(size * 1.12)

    def width(self, text):
        w = 0
        for is_e, s in _runs(text):
            if is_e:
                w += len(_emoji_clusters(s)) * (self.esize + 4)
            else:
                w += self.font.getlength(s)
        return w

    def wrap(self, text):
        lines = []
        for para in text.split("\n"):
            words, cur = para.split(" "), ""
            for wd in words:
                trial = (cur + " " + wd).strip()
                if self.width(trial) <= MAX_TEXT_W or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = wd
            lines.append(cur)
        return lines

    def draw_line(self, img, text, y):
        """Draw one line centered at baseline-ish top y. Returns nothing."""
        x = (W - self.width(text)) / 2
        d = ImageDraw.Draw(img)
        for is_e, s in _runs(text):
            if not is_e:
                d.text((x, y), s, font=self.font, fill=(255, 255, 255, 255))
                x += self.font.getlength(s)
                continue
            for cl in _emoji_clusters(s):
                tile = Image.new("RGBA", (EMOJI_NATIVE * 2, EMOJI_NATIVE * 2), (0, 0, 0, 0))
                ImageDraw.Draw(tile).text((0, 0), cl, font=self.efont, embedded_color=True)
                bbox = tile.getbbox()
                if bbox:
                    tile = tile.crop(bbox).resize((self.esize, self.esize), Image.LANCZOS)
                    img.alpha_composite(tile, (int(x + 2), int(y + self.size * 0.02)))
                x += self.esize + 4


def strip_emoji(text):
    return " ".join("".join(s for is_e, s in _runs(text) if not is_e).split(" ")).strip()


def render_caption(text, out_path, center_y=int(H * 0.27), size=64, watermark=None, allow_emoji=False):
    """allow_emoji=False (default): emoji are left out of the video. Only iOS emoji are
    acceptable on screen, and this machine can't draw them; they stay in the post caption."""
    if not allow_emoji:
        text = "\n".join(" ".join(strip_emoji(l).split()) for l in text.split("\n"))
    r = _Renderer(size)
    lines = r.wrap(text)
    line_h = int(size * 1.28)
    top = center_y - (len(lines) * line_h) // 2

    textlayer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for i, ln in enumerate(lines):
        r.draw_line(textlayer, ln, top + i * line_h)

    # Soft shadow from the text's alpha, like Instagram's native text shadow.
    alpha = textlayer.getchannel("A")
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow.putalpha(alpha.point(lambda a: int(a * 0.55)))
    shadow = shadow.filter(ImageFilter.GaussianBlur(7))
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    canvas.alpha_composite(shadow, (0, 3))
    canvas.alpha_composite(textlayer)

    if watermark:
        # Owner's watermark style: big bold white @handle near the bottom, dark outline.
        wf = ImageFont.truetype(TEXT_FONT, 34)
        wl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        wd = ImageDraw.Draw(wl)
        ww = wf.getlength(watermark)
        wd.text(((W - ww) / 2, int(H * 0.945)), watermark, font=wf, fill=(255, 255, 255, 190),
                stroke_width=2, stroke_fill=(40, 40, 40, 150))
        canvas.alpha_composite(wl)

    canvas.save(out_path)
    return out_path

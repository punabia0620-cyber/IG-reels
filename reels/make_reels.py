#!/usr/bin/env python3
"""Build vertical 9:16 Instagram/TikTok Reels from pet clips with ffmpeg.

Usage:
    python3 reels/make_reels.py plans/batch.json            # build every reel in the plan
    python3 reels/make_reels.py plans/batch.json --only r001

A plan file looks like plans/example_batch.json. Each reel lists clip segments,
an on-screen hook (shown for the first seconds), optional timed captions,
optional background music, and the post caption / hashtags / Shopee link.

Output per reel, in out/<batch>/:
    <id>.mp4        1080x1920, 30fps, H.264 + AAC, ready to upload
    <id>.txt        caption + hashtags (+ Shopee link reminder for the basket)
"""
import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unicodedata
from pathlib import Path

W, H, FPS = 1080, 1920, 30
FONT = "/usr/share/fonts/opentype/inter/Inter-ExtraBold.otf"
# Instagram covers roughly the top 14% and bottom 35% of a Reel with UI,
# so text stays in the middle band (the 4:5 safe zone).
HOOK_Y = int(H * 0.20)
CAPTION_Y = int(H * 0.58)


def run(cmd):
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr[-3000:])
        raise SystemExit(f"ffmpeg failed: {' '.join(cmd[:6])} ...")


def has_audio(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=index", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return bool(res.stdout.strip())


def hdr_transfer(path):
    res = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=color_transfer", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    t = res.stdout.strip()
    return t if t in ("arib-std-b67", "smpte2084") else None


def tonemap_chain(transfer):
    """iPhone HDR (HLG/PQ) -> normal SDR colors, so Reels don't look washed out."""
    if not transfer:
        return ""
    return (f"zscale=tin={transfer}:min=bt2020nc:pin=bt2020:t=linear:npl=100,"
            f"format=gbrpf32le,zscale=p=bt709,tonemap=hable:desat=0,"
            f"zscale=t=bt709:m=bt709:r=tv,format=yuv420p,")


def atempo_chain(speed):
    """atempo only accepts 0.5-2.0, so chain it for bigger changes."""
    parts, sp = [], speed
    while sp > 2.0:
        parts.append("atempo=2.0"); sp /= 2.0
    while sp < 0.5:
        parts.append("atempo=0.5"); sp /= 0.5
    parts.append(f"atempo={sp:.4f}")
    return ",".join(parts)


def seg_length(c):
    return c["dur"] / c.get("speed", 1.0) + c.get("freeze", 0)


def render_segment(src, c, out):
    """Trim one clip, fit it to 9:16 and apply its effects.

    Clip options: start, dur, speed (0.5 = slow-mo, 2 = fast), zoom (punch-in, e.g. 1.3),
    freeze (seconds to hold the last frame), flash (white flash in), mute (drop audio).
    Landscape/square clips get a blurred fill.
    """
    start, dur = c.get("start", 0), c["dur"]
    speed, zoom = c.get("speed", 1.0), c.get("zoom", 1.0)
    freeze, length = c.get("freeze", 0), seg_length(c)
    post = []
    if zoom != 1.0:
        post.append(f"crop=iw/{zoom}:ih/{zoom}:(iw-iw/{zoom})*{c.get('zoom_x', 0.5)}:(ih-ih/{zoom})*{c.get('zoom_y', 0.5)},scale={W}:{H}")
    if speed != 1.0:
        post.append(f"setpts=PTS/{speed}")
    if freeze:
        post.append(f"tpad=stop_mode=clone:stop_duration={freeze}")
    if c.get("flash"):
        post.append("fade=t=in:st=0:d=0.25:color=white")
    post_chain = ("," + ",".join(post)) if post else ""
    vf = (
        f"[0:v]{tonemap_chain(hdr_transfer(src))}fps={FPS},split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.08[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{post_chain},format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", str(src),
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    if has_audio(src) and not c.get("mute"):
        af = []
        if speed != 1.0:
            af.append(atempo_chain(speed))
        af.append(f"apad=whole_dur={length}")
        vf += f";[0:a:0]{','.join(af)}[aout]"
        amap = ["-map", "[aout]"]
    else:
        amap = ["-map", "1:a:0"]
    cmd += ["-filter_complex", vf, "-map", "[v]", *amap, "-t", f"{length:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)]
    run(cmd)


def strip_emoji(text):
    """The on-screen font has no emoji glyphs (they render as boxes), so drop them.
    Emoji stay in the post caption, where Instagram renders them."""
    keep = [ch for ch in text
            if ord(ch) <= 0xFFFF and unicodedata.category(ch) not in ("So", "Cs", "Mn")
            and ch not in "\u200d\ufe0f"]
    return " ".join("".join(keep).split())


STYLES = {
    # name: (fontsize, fontcolor, box, border)
    "box":   (62, "black", True, 0),     # white box, black text
    "meme":  (86, "white", False, 8),    # big white text, black outline
    "title": (150, "0xFFD60A", False, 10),  # huge yellow name card
    "sub":   (60, "white", False, 6),
    "plain": (64, "white", False, 4),    # natural meme text, Instagram-native look
}


def text_filters(text, y0, start, end, style, tmp, tag, pop=None):
    """Centered text, one drawtext per line. pop=True scales the text in quickly."""
    size, color, box, border = STYLES[style]
    width = {"title": 10, "meme": 18, "box": 24, "sub": 26, "plain": 24}[style]
    lines = [l for part in text.split("\n") for l in textwrap.wrap(strip_emoji(part), width)]
    filters, step = [], int(size * 1.3)
    for i, line in enumerate(lines):
        tf = tmp / f"{tag}_{i}.txt"
        tf.write_text(line, encoding="utf-8")
        if pop is None:
            pop = style != "plain"
        fs = f"'{size}*min(1,0.55+(t-{start})*5)'" if pop else str(size)
        look = (f"box=1:boxcolor=white@0.92:boxborderw=18" if box
                else f"borderw={border}:bordercolor=black")
        filters.append(
            f"drawtext=fontfile={FONT}:textfile={tf}:fontsize={fs}:fontcolor={color}:{look}:"
            f"x=(w-text_w)/2:y={y0 + i * step}-text_h/2+{size}/2:enable='between(t,{start},{end})'"
        )
    return filters


Y_POS = {"top": HOOK_Y, "middle": int(H * 0.40), "lower": CAPTION_Y}


def build_reel(reel, out_dir, root):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        segs = []
        for i, c in enumerate(reel["clips"]):
            seg = tmp / f"seg{i:02d}.mp4"
            render_segment(root / c["file"], c, seg)
            segs.append(seg)
        total = sum(seg_length(c) for c in reel["clips"])

        listfile = tmp / "list.txt"
        listfile.write_text("".join(f"file '{s}'\n" for s in segs))
        joined = tmp / "joined.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", str(joined)])

        filters = []
        default = reel.get("text_style", "box")
        hook = reel.get("hook")
        if hook:
            filters += text_filters(hook, HOOK_Y, 0, reel.get("hook_secs", 3.5),
                                    reel.get("hook_style", default), tmp, "hook")
        for j, cap in enumerate(reel.get("captions", [])):
            filters += text_filters(cap["text"], Y_POS[cap.get("pos", "lower")], cap["start"], cap["end"],
                                    cap.get("style", default), tmp, f"cap{j}")
        # Text attached to a clip is timed to that clip automatically.
        t0 = 0.0
        for i, c in enumerate(reel["clips"]):
            texts = c.get("text", [])
            for k, tx in enumerate(texts if isinstance(texts, list) else [texts]):
                a = t0 + tx.get("at", 0)
                b = t0 + seg_length(c) if "until" not in tx else t0 + tx["until"]
                filters += text_filters(tx["text"], Y_POS[tx.get("pos", "lower")], round(a, 3), round(b, 3),
                                        tx.get("style", default), tmp, f"c{i}t{k}")
            t0 += seg_length(c)
        vf = ",".join(filters) if filters else "null"

        out = out_dir / f"{reel['id']}.mp4"
        cmd = ["ffmpeg", "-y", "-i", str(joined)]
        music = reel.get("music")
        if music:
            vol = reel.get("music_volume", 0.35)
            cmd += ["-stream_loop", "-1", "-i", str(root / music),
                    "-filter_complex",
                    f"[0:v]{vf}[v];[1:a]volume={vol},afade=t=out:st={max(total - 1, 0)}:d=1[m];"
                    f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                    "-map", "[v]", "-map", "[a]"]
        else:
            cmd += ["-vf", vf, "-map", "0:v", "-map", "0:a"]
        cmd += ["-t", str(total), "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-maxrate", "12M", "-bufsize", "24M",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", str(out)]
        run(cmd)

    caption = reel.get("caption", "").strip()
    tags = " ".join("#" + t.lstrip("#") for t in reel.get("hashtags", []))
    body = f"{caption}\n\n{tags}".strip() + "\n"
    if reel.get("shopee_link"):
        body += f"\n[SHOPEE BASKET] Add this link as the product basket when posting:\n{reel['shopee_link']}\n"
    (out_dir / f"{reel['id']}.txt").write_text(body, encoding="utf-8")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plan")
    ap.add_argument("--only", help="build just this reel id")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg is not installed")
    plan_path = Path(args.plan)
    plan = json.loads(plan_path.read_text())
    root = Path(plan.get("clips_root", ".")).resolve()
    out_dir = Path(args.out) / plan.get("batch", plan_path.stem)
    out_dir.mkdir(parents=True, exist_ok=True)

    for reel in plan["reels"]:
        if args.only and reel["id"] != args.only:
            continue
        out = build_reel(reel, out_dir, root)
        print(f"built {out}")


if __name__ == "__main__":
    main()

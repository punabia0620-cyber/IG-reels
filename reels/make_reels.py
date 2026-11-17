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


def render_segment(src, start, dur, out):
    """Trim one clip and fit it to 9:16. Landscape/square clips get a blurred fill."""
    vf = (
        f"[0:v]fps={FPS},split[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=30:2,eq=brightness=-0.08[bg];"
        f"[b]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[v]"
    )
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(dur), "-i", str(src)]
    if has_audio(src):
        amap = ["-map", "0:a:0"]
    else:
        cmd += ["-f", "lavfi", "-t", str(dur), "-i", "anullsrc=r=44100:cl=stereo"]
        amap = ["-map", "1:a:0"]
    cmd += ["-filter_complex", vf, "-map", "[v]", *amap,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", str(out)]
    run(cmd)


def text_filters(lines, y0, start, end, size, tmp, tag):
    """One drawtext per line: white rounded-look box, black text, centered."""
    filters = []
    step = int(size * 1.45)
    for i, line in enumerate(lines):
        tf = tmp / f"{tag}_{i}.txt"
        tf.write_text(line, encoding="utf-8")
        filters.append(
            f"drawtext=fontfile={FONT}:textfile={tf}:fontsize={size}:fontcolor=black:"
            f"box=1:boxcolor=white@0.92:boxborderw=18:"
            f"x=(w-text_w)/2:y={y0 + i * step}:enable='between(t,{start},{end})'"
        )
    return filters


def build_reel(reel, out_dir, root):
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        segs = []
        for i, c in enumerate(reel["clips"]):
            seg = tmp / f"seg{i:02d}.mp4"
            render_segment(root / c["file"], c.get("start", 0), c["dur"], seg)
            segs.append(seg)
        total = sum(c["dur"] for c in reel["clips"])

        listfile = tmp / "list.txt"
        listfile.write_text("".join(f"file '{s}'\n" for s in segs))
        joined = tmp / "joined.mp4"
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(listfile),
             "-c", "copy", str(joined)])

        filters = []
        hook = reel.get("hook")
        if hook:
            lines = textwrap.wrap(hook, 20)
            filters += text_filters(lines, HOOK_Y, 0, reel.get("hook_secs", 3.5), 70, tmp, "hook")
        for j, cap in enumerate(reel.get("captions", [])):
            lines = textwrap.wrap(cap["text"], 24)
            filters += text_filters(lines, CAPTION_Y, cap["start"], cap["end"], 56, tmp, f"cap{j}")
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
        cmd += ["-t", str(total), "-c:v", "libx264", "-preset", "medium", "-crf", "19",
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

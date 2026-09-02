#!/usr/bin/env python3
"""把音樂 + SRT 歌詞 + 一張背景圖合成動態歌詞影片（MP4）。

用法:
    python3 make_lyric_video.py <音檔> <srt> <背景圖> [-o out.mp4] [--title 歌名]

效果:
    - 背景圖做 Ken Burns 慢速推鏡 + 輕微暗角，開頭淡入、結尾淡出
    - 預設用翩翩體手寫字，片頭歌名與歌詞逐句淡入上滑、淡出
    - --font pingfang 換成蘋方（歌名用宋體），--stroke 3 加白字黑邊描邊
    - --font 也可直接給自備字型檔（.ttf/.otf/.ttc），例如開源的辰宇落雁體
    - 文字帶柔和光暈（歌詞：極光青；歌名：暖粉，呼應圖中的心）
    - 底部有隨音樂跳動的鏡像頻譜（極光青→粉紫漸層），畫面飄著閃爍的雪花光點粒子
      （--no-spectrum / --no-particles 可關閉，--particles N 調數量）

實作：ffmpeg 未編入 libass，所以文字先用 PIL 畫成透明 PNG，
再用 ffmpeg 的 fade(alpha)+overlay 疊到背景上，最後與音訊一起編碼。
"""

import argparse
import glob
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

WIDTH, HEIGHT = 1920, 1080

# ---------- 字型 ----------

_ASSET_FONTS = "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/"

# 每個預設組：lyric / title 各自的候選 (檔案 glob, ttc face index)，依序找第一個存在的
FONT_PRESETS: dict[str, dict[str, list[tuple[str, int]]]] = {
    "pingfang": {   # 蘋方繁體（歌詞）+ 宋體（歌名）：乾淨現代
        "lyric": [("/System/Library/Fonts/PingFang.ttc", 2),
                  (_ASSET_FONTS + "PingFang.ttc", 2),
                  ("/System/Library/Fonts/Supplemental/Songti.ttc", 7)],
        "title": [("/System/Library/Fonts/Supplemental/Songti.ttc", 7),
                  ("/System/Library/Fonts/PingFang.ttc", 14),
                  (_ASSET_FONTS + "PingFang.ttc", 14)],
    },
    "hanzipen": {   # 翩翩體 HanziPen TC Bold：浪漫手寫筆觸
        "lyric": [("/System/Library/Fonts/Hanzipen.ttc", 3),
                  (_ASSET_FONTS + "Hanzipen.ttc", 3)],
        "title": [("/System/Library/Fonts/Hanzipen.ttc", 3),
                  (_ASSET_FONTS + "Hanzipen.ttc", 3)],
    },
    "hannotate": {  # 手札體 Hannotate TC Bold：工整一點的手寫
        "lyric": [("/System/Library/Fonts/Hannotate.ttc", 3),
                  (_ASSET_FONTS + "Hannotate.ttc", 3)],
        "title": [("/System/Library/Fonts/Hannotate.ttc", 3),
                  (_ASSET_FONTS + "Hannotate.ttc", 3)],
    },
    "xingkai": {    # 行楷 Xingkai TC Bold：毛筆書法感
        "lyric": [("/System/Library/Fonts/Xingkai.ttc", 1),
                  (_ASSET_FONTS + "Xingkai.ttc", 1)],
        "title": [("/System/Library/Fonts/Xingkai.ttc", 1),
                  (_ASSET_FONTS + "Xingkai.ttc", 1)],
    },
}
# 手寫字型字面較小，等字級下要放大一點才與蘋方視覺相當
_FONT_SCALE = {"pingfang": 1.0, "hanzipen": 1.15, "hannotate": 1.1, "xingkai": 1.15}

_FONT_EXTS = {".ttf", ".otf", ".ttc"}

# 目前生效的字型：每種用途 (檔案路徑, face index, 字級縮放)
_active: dict[str, tuple[str, int, float]] = {}


def _resolve_preset(name: str, kind: str) -> tuple[str, int, float]:
    for pattern, index in FONT_PRESETS[name][kind]:
        for path in sorted(glob.glob(pattern)):
            return path, index, _FONT_SCALE.get(name, 1.0)
    sys.exit(f"找不到字型 {name}（{kind}），可用: {', '.join(FONT_PRESETS)}")


def _resolve_file(path_str: str, index: int) -> tuple[str, int, float]:
    path = Path(path_str).expanduser()
    if not path.exists():
        sys.exit(f"找不到字型檔: {path}")
    try:   # 先試開，確認檔案與 index 有效
        ImageFont.truetype(str(path), 40, index=index)
    except Exception as e:   # noqa: BLE001
        sys.exit(f"無法載入字型檔 {path}（index={index}）: {e}")
    return str(path), index, 1.0


def set_fonts(font: str = "hanzipen", font_index: int = 0,
              title_font: str | None = None, title_font_index: int = 0) -> None:
    """設定歌詞 / 歌名字型。

    font / title_font 可以是預設名稱（pingfang、hanzipen...），
    也可以是自己提供的字型檔路徑（.ttf / .otf / .ttc）；
    .ttc 用 *_index 指定第幾個字面。title_font 不給就跟歌詞同字型
    （預設組 pingfang 例外：歌名用宋體）。
    """
    def resolve(spec: str, index: int, kind: str) -> tuple[str, int, float]:
        if spec in FONT_PRESETS:
            return _resolve_preset(spec, kind)
        if Path(spec).expanduser().suffix.lower() in _FONT_EXTS:
            return _resolve_file(spec, index)
        sys.exit(f"未知字型 {spec}：請用預設名稱（{', '.join(FONT_PRESETS)}）"
                 f"或 .ttf/.otf/.ttc 字型檔路徑")

    _active["lyric"] = resolve(font, font_index, "lyric")
    if title_font:
        _active["title"] = resolve(title_font, title_font_index, "title")
    elif font in FONT_PRESETS:
        _active["title"] = _resolve_preset(font, "title")
    else:
        _active["title"] = _active["lyric"]


def find_font(kind: str) -> tuple[str, int, float]:
    if not _active:
        set_fonts()
    return _active[kind]


def load_font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    path, index, scale = find_font(kind)
    return ImageFont.truetype(path, int(round(size * scale)), index=index)


# ---------- SRT ----------

_TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)")


def parse_ts(s: str) -> float:
    h, m, sec, ms = _TS.match(s.strip()).groups()
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")[:3]) / 1000


def parse_srt(path: Path) -> list[tuple[float, float, str]]:
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8-sig").strip())
    entries = []
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        time_line = lines[1] if "-->" in lines[1] else lines[0]
        start, end = (parse_ts(t) for t in time_line.split("-->"))
        text = " ".join(l.strip() for l in lines[lines.index(time_line) + 1:])
        if text:
            entries.append((start, end, text))
    return entries


# ---------- 文字圖層 ----------

def render_text(text: str, kind: str, size: int, glow_rgb: tuple[int, int, int],
                fill=(255, 255, 255), max_width: int = WIDTH - 200,
                glow_radius: int = 22, glow_strength: float = 1.0,
                stroke: int = 0) -> Image.Image:
    """把一行文字畫成帶光暈的透明 PNG（只裁到文字大小 + 邊距）。

    stroke > 0 時文字加深色描邊（白字黑邊的手寫歌詞風格）。"""
    font = load_font(kind, size)
    while font.getbbox(text)[2] > max_width and size > 24:   # 太長就縮字
        size -= 4
        font = load_font(kind, size)

    pad = glow_radius * 3 + stroke
    l, t, r, b = font.getbbox(text)
    w, h = r - l + pad * 2, b - t + pad * 2
    origin = (pad - l, pad - t)

    # 光暈層：用光暈色畫字 → 高斯模糊 → 疊兩次加強
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).text(origin, text, font=font, fill=glow_rgb + (255,))
    glow = glow.filter(ImageFilter.GaussianBlur(glow_radius))
    a = glow.getchannel("A").point(lambda v: min(255, int(v * 1.6 * glow_strength)))
    glow.putalpha(a)

    # 陰影：讓文字在亮處也看得清
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((origin[0] + 2, origin[1] + 3), text, font=font,
                                fill=(0, 0, 20, 160))
    shadow = shadow.filter(ImageFilter.GaussianBlur(3))

    layer = Image.alpha_composite(glow, shadow)
    ImageDraw.Draw(layer).text(origin, text, font=font, fill=fill + (255,),
                               stroke_width=stroke, stroke_fill=(10, 12, 30, 255))
    return layer


def render_bottom_gradient() -> Image.Image:
    """下方漸層壓暗，提升歌詞可讀性。"""
    grad = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    px = grad.load()
    y0 = int(HEIGHT * 0.58)
    for y in range(y0, HEIGHT):
        k = (y - y0) / (HEIGHT - y0)
        alpha = int(150 * (k ** 1.6))
        for x in range(WIDTH):
            px[x, y] = (4, 8, 20, alpha)
    return grad


# ---------- 動畫圖層：粒子、頻譜 ----------

PARTICLE_W, PARTICLE_H = WIDTH // 2, HEIGHT // 2   # 粒子在半解析度算，放大後更柔


class ParticleField:
    """飄升、左右搖曳、明暗閃爍的雪花／光點粒子，逐幀輸出 RGBA（半解析度）。"""

    def __init__(self, count: int, seed: int = 7):
        rng = np.random.default_rng(seed)
        W, H = PARTICLE_W, PARTICLE_H
        self.n = count
        self.x0 = rng.uniform(0, W, count)
        self.y0 = rng.uniform(0, H, count)
        self.speed = rng.uniform(6, 22, count)                 # px/s 向上
        self.sway_amp = rng.uniform(4, 18, count)
        self.sway_f = rng.uniform(0.1, 0.35, count)
        self.sway_p = rng.uniform(0, 2 * np.pi, count)
        self.size = rng.choice([2, 3, 4, 6, 9, 14], count,
                               p=[.25, .25, .2, .15, .1, .05])
        self.bright = rng.uniform(0.35, 1.0, count) * np.where(self.size >= 9, 0.7, 1.0)
        self.tw_f = rng.uniform(0.3, 1.2, count)
        self.tw_p = rng.uniform(0, 2 * np.pi, count)
        self.warm = rng.random(count) < 0.18                    # 少數暖粉色，呼應心
        self.sprites = {r: self._sprite(r) for r in set(self.size.tolist())}
        self.cold = np.array([220, 240, 255], np.float32)
        self.pink = np.array([255, 190, 205], np.float32)

    @staticmethod
    def _sprite(r: int) -> np.ndarray:
        d = np.arange(-r * 2, r * 2 + 1)
        yy, xx = np.meshgrid(d, d, indexing="ij")
        g = np.exp(-(xx ** 2 + yy ** 2) / (2 * (r * 0.9) ** 2))
        return (g / g.max()).astype(np.float32)

    def frame(self, t: float) -> bytes:
        W, H = PARTICLE_W, PARTICLE_H
        acc = np.zeros((H, W, 3), np.float32)
        alpha = np.zeros((H, W), np.float32)
        ys = (self.y0 - self.speed * t) % (H + 40) - 20
        xs = (self.x0 + self.sway_amp * np.sin(2 * np.pi * self.sway_f * t + self.sway_p)) % W
        tw = 0.55 + 0.45 * np.sin(2 * np.pi * self.tw_f * t + self.tw_p)
        for i in range(self.n):
            sp = self.sprites[int(self.size[i])]
            r = sp.shape[0] // 2
            x1, y1 = int(xs[i]) - r, int(ys[i]) - r
            x2, y2 = x1 + sp.shape[1], y1 + sp.shape[0]
            sx1, sy1 = max(0, -x1), max(0, -y1)
            sx2, sy2 = sp.shape[1] - max(0, x2 - W), sp.shape[0] - max(0, y2 - H)
            if sx2 <= sx1 or sy2 <= sy1:
                continue
            s = sp[sy1:sy2, sx1:sx2] * (self.bright[i] * tw[i])
            col = self.pink if self.warm[i] else self.cold
            ys_, xs_ = slice(max(0, y1), min(H, y2)), slice(max(0, x1), min(W, x2))
            acc[ys_, xs_] += s[:, :, None] * col
            alpha[ys_, xs_] += s
        alpha = np.clip(alpha, 0, 1)
        rgb = np.clip(acc / np.maximum(alpha, 1e-3)[:, :, None], 0, 255)
        out = np.empty((H, W, 4), np.uint8)
        out[:, :, :3] = rgb
        out[:, :, 3] = (alpha * 255).astype(np.uint8)
        return out.tobytes()


def render_spectrum_mask(height: int) -> Image.Image:
    """頻譜用的遮罩：直條紋 × 水平色彩漸層（中心極光青→兩側粉紫）× 上方淡出。"""
    W, H = WIDTH, height
    x = np.arange(W)
    y = np.arange(H)[:, None]
    stripe = ((x % 12) < 8).astype(np.float32)[None, :]          # 8px 亮 4px 暗
    t = np.abs(x - W / 2) / (W / 2)
    c_center = np.array([150, 240, 215], np.float32)
    c_edge = np.array([230, 150, 200], np.float32)
    col = c_center[None, :] * (1 - t[:, None]) + c_edge[None, :] * t[:, None]
    vfade = np.clip((y / H) ** 0.5, 0, 1).astype(np.float32)
    rgb = (col[None, :, :] * stripe[:, :, None] * vfade[:, :, None]).astype(np.uint8)
    return Image.fromarray(rgb, "RGB")


# ---------- ffmpeg ----------

def probe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def build(audio: Path, srt: Path, image: Path, out: Path, title: str,
          fps: int = 30, lyric_size: int = 66, title_size: int = 120, crf: int = 18,
          font: str = "hanzipen", stroke: int = 0, font_index: int = 0,
          title_font: str | None = None, title_font_index: int = 0,
          spectrum: bool = True, particles: int = 160,
          spectrum_height: int = 150) -> None:
    set_fonts(font, font_index, title_font, title_font_index)
    entries = parse_srt(srt)
    if not entries:
        sys.exit("SRT 裡沒有任何字幕")
    duration = probe_duration(audio)
    total_frames = int(duration * fps) + 1

    work = Path(tempfile.mkdtemp(prefix="lyricvideo_"))
    try:
        # 1. 文字圖層
        overlays: list[dict] = []   # {path, start, end, y, slide, fade_in, fade_out}

        grad_path = work / "gradient.png"
        render_bottom_gradient().save(grad_path)

        first_start = entries[0][0]
        title_start, title_end = 1.2, max(first_start - 0.8, 4.0)
        if title:
            img = render_text(title, "title", title_size, glow_rgb=(255, 140, 150),
                              fill=(255, 246, 240), glow_radius=30, glow_strength=1.1,
                              stroke=stroke)
            p = work / "title.png"
            img.save(p)
            overlays.append(dict(path=p, start=title_start, end=title_end,
                                 y="(H-h)/2-30", slide=18, fade_in=1.4, fade_out=1.0))

        lyric_y = "H*0.80-h/2"
        for i, (start, end, text) in enumerate(entries):
            img = render_text(text, "lyric", lyric_size, glow_rgb=(120, 225, 205),
                              stroke=stroke)
            p = work / f"line_{i:03d}.png"
            img.save(p)
            dur = end - start
            overlays.append(dict(path=p, start=start, end=end, y=lyric_y, slide=26,
                                 fade_in=min(0.5, dur / 3), fade_out=min(0.45, dur / 3)))

        # 2. filter graph
        inputs = ["-i", str(image)]
        chains = []
        # 背景：先放大避免 zoompan 抖動，再慢速推鏡 1.0 → 1.10，最後暗角
        chains.append(
            f"[0:v]scale={WIDTH*2}:-2,"
            f"zoompan=z='1+0.10*on/{total_frames}':d={total_frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)+ih*0.02*on/{total_frames}'"
            f":s={WIDTH}x{HEIGHT}:fps={fps},"
            f"vignette=angle=PI/5,format=rgba[bg]")

        inputs += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(grad_path)]
        chains.append("[bg][1:v]overlay=0:0:format=auto[v0]")
        idx, cur, step = 2, "v0", 0

        def next_label() -> str:
            nonlocal step
            step += 1
            return f"v{step}"

        # 粒子層：由 Python 逐幀經 stdin 送進來（半解析度 RGBA），放大後疊上
        field = None
        if particles > 0:
            field = ParticleField(particles)
            inputs += ["-f", "rawvideo", "-pix_fmt", "rgba",
                       "-s", f"{PARTICLE_W}x{PARTICLE_H}", "-r", str(fps),
                       "-i", "pipe:0"]
            nxt = next_label()
            chains.append(f"[{idx}:v]scale={WIDTH}:{HEIGHT}:flags=bilinear,format=rgba[pt]")
            chains.append(f"[{cur}][pt]overlay=0:0:format=auto:eof_action=pass[{nxt}]")
            cur, idx = nxt, idx + 1

        # 頻譜：showfreqs → 左右鏡像（低音在中間）→ 乘上條紋漸層遮罩 → 去黑底
        spec_mask_idx = None
        if spectrum:
            mask_path = work / "spectrum_mask.png"
            render_spectrum_mask(spectrum_height).save(mask_path)
            inputs += ["-loop", "1", "-t", f"{duration:.3f}", "-i", str(mask_path)]
            spec_mask_idx, idx = idx, idx + 1

        for k, ov in enumerate(overlays):
            dur = ov["end"] - ov["start"]
            inputs += ["-loop", "1", "-t", f"{dur:.3f}", "-i", str(ov["path"])]
            ov["idx"] = idx
            idx += 1

        inputs += ["-i", str(audio)]
        audio_idx = idx

        if spectrum:
            chains.append(f"[{audio_idx}:a]asplit[a_out][a_spec]")
            chains.append(
                f"[a_spec]aformat=channel_layouts=mono,volume=0.7,"
                f"showfreqs=mode=bar:ascale=log:fscale=log"
                f":size={WIDTH//2}x{spectrum_height}:rate={fps}"
                f":win_size=1024:averaging=4:colors=0xffffff[s]")
            chains.append("[s]split[s1][s2];[s2]hflip[s3];[s3][s1]hstack,format=rgb24[spec]")
            chains.append(f"[{spec_mask_idx}:v]format=rgb24[spm]")
            chains.append(
                "[spec][spm]blend=all_mode=multiply:shortest=1,"
                "colorkey=0x000000:0.15:0.05,format=rgba,colorchannelmixer=aa=0.85[sp]")
            nxt = next_label()
            chains.append(f"[{cur}][sp]overlay=0:H-h:format=auto:eof_action=pass[{nxt}]")
            cur = nxt
            audio_map = "[a_out]"
        else:
            audio_map = f"{audio_idx}:a"

        for k, ov in enumerate(overlays):
            dur = ov["end"] - ov["start"]
            fi, fo = ov["fade_in"], ov["fade_out"]
            chains.append(
                f"[{ov['idx']}:v]format=rgba,"
                f"fade=t=in:st=0:d={fi:.3f}:alpha=1,"
                f"fade=t=out:st={max(dur - fo, 0):.3f}:d={fo:.3f}:alpha=1,"
                f"setpts=PTS+{ov['start']:.3f}/TB[ov{k}]")
            nxt = next_label()
            # 上滑：前 fade_in 秒從 +slide px 滑到定位
            y_expr = f"{ov['y']}+{ov['slide']}*(1-min(1,(t-{ov['start']:.3f})/{fi:.3f}))"
            chains.append(
                f"[{cur}][ov{k}]overlay=x=(W-w)/2:y='{y_expr}'"
                f":enable='between(t,{ov['start']:.3f},{ov['end']:.3f})'"
                f":eof_action=pass:format=auto[{nxt}]")
            cur = nxt

        fade_out_start = max(duration - 2.5, 0)
        chains.append(
            f"[{cur}]fade=t=in:st=0:d=1.5,fade=t=out:st={fade_out_start:.3f}:d=2.5,"
            f"format=yuv420p[vout]")

        script = work / "filter.txt"
        script.write_text(";\n".join(chains), encoding="utf-8")

        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-stats",
               *inputs,
               "-filter_complex_script", str(script),
               "-map", "[vout]", "-map", audio_map,
               "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
               "-r", str(fps), "-movflags", "+faststart",
               "-c:a", "aac", "-b:a", "192k",
               "-t", f"{duration:.3f}",
               str(out)]
        extras = []
        if field is not None:
            extras.append(f"{particles} 顆粒子")
        if spectrum:
            extras.append("音樂頻譜")
        print(f"合成影片中（{len(entries)} 句歌詞，{duration:.1f} 秒，{fps} fps"
              + (f"，{'、'.join(extras)}" if extras else "") + "）...")

        if field is None:
            subprocess.run(cmd, check=True)
        else:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            try:
                for n in range(total_frames):
                    proc.stdin.write(field.frame(n / fps))
            except BrokenPipeError:
                pass     # ffmpeg 已結束（多半是出錯），下面用 returncode 回報
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            if proc.wait() != 0:
                sys.exit(f"ffmpeg 失敗（exit {proc.returncode}）")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="音樂 + SRT + 圖片 → 動態歌詞影片")
    ap.add_argument("audio")
    ap.add_argument("srt")
    ap.add_argument("image")
    ap.add_argument("-o", "--output", default=None, help="輸出 mp4（預設與音檔同名）")
    ap.add_argument("--title", default=None,
                    help="片頭歌名（預設用音檔檔名；傳空字串則不顯示）")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--lyric-size", type=int, default=66)
    ap.add_argument("--title-size", type=int, default=120)
    ap.add_argument("--crf", type=int, default=18, help="畫質，越小越好（預設 18）")
    ap.add_argument("--font", default="hanzipen",
                    help="歌詞字型：預設名稱 hanzipen 翩翩體手寫（預設）、pingfang 蘋方、"
                         "hannotate 手札體、xingkai 行楷；或自備字型檔路徑（.ttf/.otf/.ttc）")
    ap.add_argument("--font-index", type=int, default=0,
                    help="自備 .ttc 字型檔時要用第幾個字面（預設 0）")
    ap.add_argument("--title-font", default=None,
                    help="片頭歌名字型（預設名稱或字型檔）；不給則跟歌詞同字型")
    ap.add_argument("--title-font-index", type=int, default=0)
    ap.add_argument("--no-spectrum", action="store_true", help="不顯示音樂頻譜")
    ap.add_argument("--no-particles", action="store_true", help="不顯示粒子")
    ap.add_argument("--particles", type=int, default=160, help="粒子數量（預設 160）")
    ap.add_argument("--spectrum-height", type=int, default=150,
                    help="頻譜高度 px（預設 150）")
    ap.add_argument("--stroke", type=int, default=0,
                    help="文字描邊粗細（px），例如 3 = 白字黑邊手寫風；預設 0 只有光暈")
    args = ap.parse_args()

    audio, srt, image = Path(args.audio), Path(args.srt), Path(args.image)
    for p in (audio, srt, image):
        if not p.exists():
            sys.exit(f"找不到檔案: {p}")
    if not shutil.which("ffmpeg"):
        sys.exit("需要 ffmpeg，請先 brew install ffmpeg")

    out = Path(args.output) if args.output else audio.with_suffix(".mp4")
    title = audio.stem if args.title is None else args.title
    build(audio, srt, image, out, title, args.fps,
          args.lyric_size, args.title_size, args.crf, args.font, args.stroke,
          args.font_index, args.title_font, args.title_font_index,
          spectrum=not args.no_spectrum,
          particles=0 if args.no_particles else args.particles,
          spectrum_height=args.spectrum_height)
    print(f"\n完成！影片: {out}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""辨識本機音檔（mp3/wav/m4a...）產生歌詞 SRT 字幕檔。

用法:
    python3 audio_to_srt.py <音檔路徑> [歌詞txt] [選項]

範例:
    python3 audio_to_srt.py "song.mp3"                # 純辨識，字幕用辨識文字
    python3 audio_to_srt.py "song.mp3" "lyrics.txt"   # 對齊模式，字幕用歌詞原文
    python3 audio_to_srt.py "song.mp3" --model medium --language zh
    python3 audio_to_srt.py "song.mp3" "lyrics.txt" --image bg.png   # 再合成動態歌詞影片

歌詞 txt 格式：一行一句字幕，空行（段落分隔）會自動忽略。
"""

import argparse
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def format_timestamp(seconds: float) -> str:
    """秒數轉 SRT 時間格式 HH:MM:SS,mmm"""
    ms = int(round(max(seconds, 0) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(srt_path: Path, entries: list[tuple[float, float, str]]) -> None:
    """entries: [(start, end, text), ...]"""
    with srt_path.open("w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(entries, start=1):
            f.write(f"{i}\n")
            f.write(f"{format_timestamp(start)} --> {format_timestamp(end)}\n")
            f.write(f"{text}\n\n")


def load_model(model_size: str):
    from faster_whisper import WhisperModel
    print(f"載入 Whisper 模型 ({model_size})...")
    return WhisperModel(model_size, device="cpu", compute_type="int8")


try:
    from opencc import OpenCC
    _T2S = OpenCC("t2s")
except ImportError:  # 沒裝 opencc 時退回不轉換（簡繁不一致的字會被視為聽錯）
    _T2S = None


def normalize_chars(text: str) -> list[str]:
    """只留字母、數字、CJK 字元，統一成簡體並轉小寫，供比對用。

    Whisper 常輸出簡繁混雜（如「长」「让」「见」），若不統一，
    繁體歌詞就對不到這些字，句子起訖時間會跟著跑掉。
    """
    if _T2S is not None:
        text = _T2S.convert(text)
    return [unicodedata.normalize("NFKC", c).lower()
            for c in text if c.isalnum()]


def load_lyrics(path: Path) -> list[str]:
    """讀歌詞檔：一行一句，忽略空行。"""
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    return [line for line in lines if line]


def transcribe(model, audio_path: Path, language: str | None,
               use_vad: bool, word_timestamps: bool):
    """跑 Whisper 辨識，回傳 segments list 與 info。

    注意：歌曲不要開 VAD——VAD 常把「有伴奏的歌聲」誤判為非人聲，
    導致辨識中途截斷；VAD 只適合純語音（podcast、訪談）。
    """
    print("語音辨識中（音檔較長時需要幾分鐘）...")
    segments, info = model.transcribe(
        str(audio_path),
        language=language,                 # None = 自動偵測
        vad_filter=use_vad,
        condition_on_previous_text=False,  # 避免歌曲重複段落造成迴圈中斷
        word_timestamps=word_timestamps,
        beam_size=5,
    )
    segments = list(segments)
    print(f"偵測語言: {info.language} (機率 {info.language_probability:.2f})")
    return segments


def srt_from_recognition(segments) -> list[tuple[float, float, str]]:
    """純辨識模式：直接用 Whisper 的斷句與文字。"""
    entries = []
    for seg in segments:
        text = seg.text.strip()
        if text:
            entries.append((seg.start, seg.end, text))
    return entries


def align_lyrics_to_audio(lyric_lines: list[str],
                          segments) -> list[tuple[float, float, str]]:
    """對齊模式：把歌詞逐行對齊到 Whisper 的逐字時間軸。

    做法：把辨識結果攤平成「字元 + 時間」序列，與歌詞字元序列做
    編輯距離 DP 對齊（Needleman-Wunsch），每句歌詞取其匹配字元的
    最早/最晚時間當作字幕起訖；沒對到的句子用前後句時間內插。
    """
    # 1. 辨識結果 → 帶時間的字元流（一個詞的時間平均分給它的字元）
    rec_chars: list[tuple[str, float, float]] = []
    for seg in segments:
        for w in (seg.words or []):
            chars = normalize_chars(w.word)
            if not chars:
                continue
            dur = (w.end - w.start) / len(chars)
            for i, c in enumerate(chars):
                rec_chars.append((c, w.start + i * dur, w.start + (i + 1) * dur))

    if not rec_chars:
        sys.exit("辨識不到任何人聲，無法對齊歌詞")

    # 2. 歌詞 → 字元流（記住每個字元屬於第幾句）
    lyr_chars: list[tuple[str, int]] = []
    for li, line in enumerate(lyric_lines):
        for c in normalize_chars(line):
            lyr_chars.append((c, li))

    # 3. DP 對齊（match=0，mismatch/gap=1），回溯取得字元對應
    n, m = len(lyr_chars), len(rec_chars)
    INF = 10 ** 9
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        lc = lyr_chars[i - 1][0]
        row, prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            cost = 0 if lc == rec_chars[j - 1][0] else 1
            row[j] = min(prev[j - 1] + cost,  # match / substitute
                         prev[j] + 1,         # 歌詞字元沒被唱到（gap）
                         row[j - 1] + 1)      # 辨識多出的字元（gap）

    # 回溯：match 與「替換」（位置對上但字被聽錯，如 原來→眼愛）都記錄時間。
    # 替換代表那個位置確實有人聲，只是辨識錯字；若丟掉，句首/句尾被聽錯時
    # 整句時間會往內縮（起點延後、結尾提早）。
    line_times: dict[int, list[float]] = {}
    i, j = n, m
    while i > 0 and j > 0:
        lc, li = lyr_chars[i - 1]
        rc, rs, re_ = rec_chars[j - 1]
        cost = 0 if lc == rc else 1
        if dp[i][j] == dp[i - 1][j - 1] + cost:
            line_times.setdefault(li, []).extend([rs, re_])
            i, j = i - 1, j - 1
        elif dp[i][j] == dp[i - 1][j] + 1:
            i -= 1
        else:
            j -= 1

    # 4. 每句取匹配時間範圍；沒對到的句子用前後內插
    total = len(lyric_lines)
    starts: list[float | None] = [None] * total
    ends: list[float | None] = [None] * total
    for li, times in line_times.items():
        starts[li], ends[li] = min(times), max(times)

    matched = [li for li in range(total) if starts[li] is not None]
    if not matched:
        sys.exit("歌詞與辨識結果完全對不起來，請確認音檔與歌詞是否相符")
    unmatched = total - len(matched)
    if unmatched:
        print(f"警告: 有 {unmatched} 句歌詞沒有直接對到人聲，時間以前後句推估")

    for li in range(total):
        if starts[li] is None:
            prev_end = next((ends[k] for k in range(li - 1, -1, -1)
                             if ends[k] is not None), 0.0)
            next_start = next((starts[k] for k in range(li + 1, total)
                               if starts[k] is not None), prev_end + 4.0)
            starts[li], ends[li] = prev_end, min(prev_end + 4.0, next_start)

    # 5. 修正重疊：起點遞增、上一句結尾不超過下一句開頭
    entries: list[tuple[float, float, str]] = []
    for li in range(total):
        start, end = starts[li], ends[li]
        if entries and start < entries[-1][1]:
            prev = entries[-1]
            entries[-1] = (prev[0], max(prev[0], start), prev[2])
        entries.append((start, max(end, start + 0.5), lyric_lines[li]))
    return entries


def main():
    parser = argparse.ArgumentParser(
        description="辨識本機音檔產生歌詞 SRT 字幕")
    parser.add_argument("audio", help="音檔路徑（mp3/wav/m4a 等 ffmpeg 支援的格式）")
    parser.add_argument("lyrics", nargs="?", default=None,
                        help="歌詞 txt（一行一句）；提供時字幕用歌詞原文並自動對齊時間")
    parser.add_argument("--model", default="small",
                        choices=["tiny", "base", "small", "medium", "large-v3"],
                        help="Whisper 模型大小，越大越準但越慢（預設 small）")
    parser.add_argument("--language", default=None,
                        help="語言代碼，如 zh、en、ja；不指定則自動偵測")
    parser.add_argument("-o", "--output", default=None,
                        help="輸出 SRT 路徑（預設與音檔同資料夾同檔名）")
    parser.add_argument("--vad", action="store_true",
                        help="啟用 VAD 過濾（僅建議純語音使用，歌曲請勿開啟）")
    parser.add_argument("--image", default=None,
                        help="背景圖；提供時會接著用 make_lyric_video.py 合成動態歌詞影片 mp4")
    parser.add_argument("--title", default=None,
                        help="影片片頭歌名（預設用音檔檔名；傳空字串則不顯示）")
    parser.add_argument("--font", default="hanzipen",
                        help="影片字型：hanzipen 翩翩體手寫（預設）、pingfang 蘋方、hannotate、xingkai，"
                             "或自備字型檔路徑（.ttf/.otf/.ttc）")
    parser.add_argument("--font-index", type=int, default=0,
                        help="自備 .ttc 字型檔時要用第幾個字面")
    parser.add_argument("--title-font", default=None,
                        help="片頭歌名字型（預設名稱或字型檔）；不給則跟歌詞同字型")
    parser.add_argument("--no-spectrum", action="store_true", help="影片不顯示音樂頻譜")
    parser.add_argument("--no-particles", action="store_true", help="影片不顯示粒子")
    parser.add_argument("--stroke", type=int, default=0,
                        help="影片文字描邊粗細 px（0 = 只有光暈）")
    args = parser.parse_args()

    audio_path = Path(args.audio)
    if not audio_path.exists():
        sys.exit(f"找不到音檔: {audio_path}")

    lyric_lines = None
    if args.lyrics:
        lyrics_path = Path(args.lyrics)
        if not lyrics_path.exists():
            sys.exit(f"找不到歌詞檔: {lyrics_path}")
        lyric_lines = load_lyrics(lyrics_path)
        print(f"歌詞: {len(lyric_lines)} 句（{lyrics_path.name}）")

    srt_path = Path(args.output) if args.output else audio_path.with_suffix(".srt")

    model = load_model(args.model)
    segments = transcribe(model, audio_path, args.language,
                          use_vad=args.vad,
                          word_timestamps=lyric_lines is not None)

    if lyric_lines is not None:
        entries = align_lyrics_to_audio(lyric_lines, segments)
    else:
        entries = srt_from_recognition(segments)

    write_srt(srt_path, entries)
    print(f"\n完成！共 {len(entries)} 句字幕")
    print(f"字幕: {srt_path}")

    if args.image:
        image_path = Path(args.image)
        if not image_path.exists():
            sys.exit(f"找不到背景圖: {image_path}")
        from make_lyric_video import build as build_video
        video_path = srt_path.with_suffix(".mp4")
        title = audio_path.stem if args.title is None else args.title
        print()
        build_video(audio_path, srt_path, image_path, video_path, title,
                    font=args.font, stroke=args.stroke,
                    font_index=args.font_index, title_font=args.title_font,
                    spectrum=not args.no_spectrum,
                    particles=0 if args.no_particles else 160)
        print(f"影片: {video_path}")


if __name__ == "__main__":
    main()

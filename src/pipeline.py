"""
KTV Maker Pipeline
YouTube 下載 → 音訊去人聲 → 字幕提取/生成 → 燒入 MP4 1080p 輸出
"""

import os
import re
import sys
import json
import shutil
import subprocess
import argparse
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

# ── 路徑設定 ──────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent.parent
TEMP_DIR   = BASE_DIR / "temp"
OUTPUT_DIR = BASE_DIR / "output"
TEMP_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════
# Step 1 │ 下載 YouTube（影片 + 音訊分離）
# ══════════════════════════════════════════════════════════
def _clean_url(url: str) -> str:
    """保留純 watch?v= 網址，移除 list/index 等播放清單參數。"""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    v = qs.get("v", [None])[0]
    if v:
        clean = parsed._replace(query=urlencode({"v": v}))
        return urlunparse(clean)
    return url


def download_youtube(url: str, job_id: str) -> dict:
    """
    下載 1080p 影片（無聲）和原始音訊（WAV），
    同時嘗試下載 YouTube 內嵌字幕。
    回傳 { video_path, audio_path, subtitle_path (or None), title }
    """
    url = _clean_url(url)
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    print("[1/4] 下載 YouTube 影片...")

    # ── 下載無聲影片（僅視訊串流 1080p）
    video_path = job_dir / "video_nosound.mp4"
    subprocess.run([
        "yt-dlp",
        "-f", "bestvideo[height<=1080][ext=mp4]/bestvideo[height<=1080]",
        "--no-audio",
        "--no-playlist",
        "-o", str(video_path),
        url
    ], check=True)

    # ── 下載原始音訊（WAV，給 Demucs 使用）
    audio_path = job_dir / "audio_original.wav"
    subprocess.run([
        "yt-dlp",
        "-f", "bestaudio",
        "--extract-audio",
        "--audio-format", "wav",
        "--audio-quality", "0",
        "--no-playlist",
        "-o", str(audio_path),
        url
    ], check=True)

    # ── 取得影片標題（用於命名輸出檔）
    result = subprocess.run([
        "yt-dlp", "--print", "title", "--no-playlist", url
    ], capture_output=True, text=True)
    title = result.stdout.strip() or job_id

    # ── 嘗試下載 YouTube 字幕（繁體中文優先）
    subtitle_path = None
    sub_out = job_dir / "subtitle"
    sub_result = subprocess.run([
        "yt-dlp",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs", "zh-Hant,zh-TW,zh,zh-Hans",
        "--sub-format", "vtt/srt/best",
        "--convert-subs", "srt",
        "--skip-download",
        "--no-playlist",
        "-o", str(sub_out),
        url
    ], capture_output=True, text=True)

    # 找到實際下載的 .srt 檔
    for f in job_dir.glob("subtitle*.srt"):
        subtitle_path = f
        print(f"  ✓ 找到 YouTube 字幕：{f.name}")
        break

    if not subtitle_path:
        print("  ⚠ 無 YouTube 字幕，稍後使用 Whisper 生成")

    return {
        "video_path":    video_path,
        "audio_path":    audio_path,
        "subtitle_path": subtitle_path,
        "title":         title,
        "job_dir":       job_dir,
    }


# ══════════════════════════════════════════════════════════
# Step 2 │ Demucs 人聲分離 → 輸出伴奏 WAV
# ══════════════════════════════════════════════════════════
def separate_vocals(audio_path: Path, job_dir: Path) -> Path:
    """
    使用 Demucs htdemucs 模型分離人聲與伴奏。
    回傳伴奏 WAV 路徑。
    """
    print("[2/4] AI 人聲分離（Demucs htdemucs）...")

    if not audio_path.exists():
        raise FileNotFoundError(f"音訊檔案不存在：{audio_path}")

    subprocess.run([
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "-n", "htdemucs",
        "--device", "cuda",
        "--out", str(job_dir / "demucs_out"),
        str(audio_path)
    ], check=True)

    # Demucs 輸出路徑：demucs_out/htdemucs/<stem_name>/no_vocals.wav
    stem_name = audio_path.stem
    instrumental = job_dir / "demucs_out" / "htdemucs" / stem_name / "no_vocals.wav"

    if not instrumental.exists():
        matches = list((job_dir / "demucs_out").rglob("no_vocals.wav"))
        if not matches:
            raise FileNotFoundError("Demucs 輸出找不到 no_vocals.wav")
        instrumental = matches[0]

    vocals = instrumental.parent / "vocals.wav"
    print(f"  ✓ 伴奏輸出：{instrumental}")
    return {
        "instrumental": instrumental,
        "vocals": vocals if vocals.exists() else None,
    }


# ══════════════════════════════════════════════════════════
# Step 3 │ Whisper 字幕生成（YouTube 字幕不存在時使用）
# ══════════════════════════════════════════════════════════
def generate_subtitles_whisper(audio_path: Path, job_dir: Path) -> Path:
    """
    使用 faster-whisper（large-v3 模型）從伴奏或原始音訊生成 SRT 字幕。
    語言設為繁體中文（zh），並做繁體轉換。
    """
    print("[3/4] Whisper AI 生成字幕...")

    try:
        from faster_whisper import WhisperModel
        import opencc
    except ImportError:
        raise ImportError(
            "請先安裝：pip install faster-whisper opencc-python-reimplemented"
        )

    cc = opencc.OpenCC("s2tw")
    ass_path = job_dir / "subtitle_whisper.ass"

    # 優先使用 Demucs 分離後的人聲（無背景音樂，辨識率更高）
    vocals_path = job_dir / "demucs_out" / "htdemucs" / audio_path.stem / "vocals.wav"
    if vocals_path.exists():
        whisper_audio = vocals_path
        use_vad = False  # 純人聲不需要 VAD
        print(f"  → 使用分離人聲軌道：{vocals_path.name}")
    else:
        whisper_audio = audio_path
        use_vad = True

    configs = [
        ("large-v3", "cuda", "int8_float16"),
        ("medium",   "cuda", "int8_float16"),
        ("medium",   "cpu",  "int8"),
    ]
    last_err = None
    for model_name, device, compute in configs:
        try:
            print(f"  → 嘗試 Whisper {model_name} on {device} ({compute})")
            model = WhisperModel(model_name, device=device, compute_type=compute)
            segments, _ = model.transcribe(
                str(whisper_audio),
                language="zh",
                beam_size=5,
                word_timestamps=True,
                vad_filter=use_vad,
                vad_parameters=dict(min_silence_duration_ms=500),
            )

            lines = [_ASS_HEADER]
            for seg in segments:
                text = cc.convert(seg.text.strip())
                words = None
                if seg.words:
                    words = [
                        {"word": cc.convert(w.word), "start": w.start, "end": w.end}
                        for w in seg.words
                    ]
                line = _karaoke_body(text, seg.start, seg.end, words)
                if line:
                    lines.append(line)

            dialogue_count = len(lines) - 1
            ass_path.write_text("".join(lines), encoding="utf-8")
            print(f"  ✓ Whisper 完成（{model_name} / {device}），共 {dialogue_count} 條字幕：{ass_path}")
            if dialogue_count == 0:
                print("  ⚠ 警告：未辨識出任何語音，字幕為空")
            return ass_path
        except Exception as e:
            print(f"  ⚠ {model_name}/{device} 失敗：{e}，嘗試下一個設定...")
            last_err = e
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:
                pass

    raise RuntimeError(f"Whisper 所有設定均失敗：{last_err}")


def _fmt_time(seconds: float) -> str:
    """浮點秒數 → SRT 時間格式 HH:MM:SS,mmm"""
    ms = int((seconds % 1) * 1000)
    s  = int(seconds) % 60
    m  = int(seconds) // 60 % 60
    h  = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _ass_time(seconds: float) -> str:
    """浮點秒數 → ASS 時間格式 H:MM:SS.cs"""
    cs  = int((seconds % 1) * 100)
    s   = int(seconds) % 60
    m   = int(seconds) // 60 % 60
    h   = int(seconds) // 3600
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


# ASS 檔案標頭：KTV 卡拉 OK 樣式
# PrimaryColour = 黃色（已唱），SecondaryColour = 白色（未唱）
_ASS_HEADER = """\
[Script Info]
ScriptType: v4.00+
WrapStyle: 0
PlayResX: 1920
PlayResY: 1080
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,Noto Sans CJK TC,58,&H0000FFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,1,3,2,2,20,20,60,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""


def _karaoke_body(text: str, start: float, end: float, words: list | None) -> str:
    """組合帶有 \\kf 標籤的 ASS 對話行本文。"""
    if words:
        body = ""
        for w in words:
            cs = max(1, int((w["end"] - w["start"]) * 100))
            body += f"{{\\kf{cs}}}{w['word']}"
    else:
        chars = list(text)
        if not chars:
            return ""
        cs_each = max(1, int((end - start) * 100 / len(chars)))
        body = "".join(f"{{\\kf{cs_each}}}{c}" for c in chars)
    return f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{body}\n"


def _srt_to_karaoke_ass(srt_path: Path, ass_path: Path) -> None:
    """將 SRT 字幕轉為 KTV 卡拉 OK ASS，時間平均分配給每個字。"""
    import re as _re
    content = srt_path.read_text(encoding="utf-8")
    blocks = [b.strip() for b in content.strip().split("\n\n") if b.strip()]

    lines = [_ASS_HEADER]
    time_re = _re.compile(
        r"(\d+:\d+:\d+[,\.]\d+)\s*-->\s*(\d+:\d+:\d+[,\.]\d+)"
    )
    for block in blocks:
        parts = block.splitlines()
        if len(parts) < 3:
            continue
        m = time_re.search(block)
        if not m:
            continue
        start_s = _srt_time_to_sec(m.group(1))
        end_s   = _srt_time_to_sec(m.group(2))
        text    = " ".join(parts[2:]).strip()
        line = _karaoke_body(text, start_s, end_s, None)
        if line:
            lines.append(line)
    ass_path.write_text("".join(lines), encoding="utf-8")


def _srt_time_to_sec(t: str) -> float:
    t = t.replace(",", ".")
    h, m, rest = t.split(":")
    return int(h) * 3600 + int(m) * 60 + float(rest)


# ══════════════════════════════════════════════════════════
# Step 4 │ FFmpeg 合成：影片 + 伴奏 + 字幕燒入 → MP4 1080p
# ══════════════════════════════════════════════════════════
def compose_output(
    video_path:        Path,
    instrumental_path: Path,
    subtitle_path:     Path | None,
    title:             str,
    job_dir:           Path,
    vocal_mix:         float = 0.0,
    vocals_path:       Path | None = None,
    subtitle_mode:     str = "auto",  # "auto" | "none"
) -> Path:
    """
    使用 FFmpeg 將無聲影片、伴奏音訊、SRT 字幕合成為最終 KTV MP4。
    subtitle_mode="none" 時跳過字幕燒入（影片畫面已有歌詞的 MV 適用）。
    """
    print("[4/4] FFmpeg 合成輸出...")

    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()
    output_path = OUTPUT_DIR / f"{safe_title}_KTV.mp4"

    # 字幕處理（mode=none 或沒有字幕檔時跳過）
    use_subtitle = subtitle_mode == "auto" and subtitle_path and subtitle_path.exists()
    if use_subtitle:
        if subtitle_path.suffix.lower() == ".ass":
            # Whisper 已產生 karaoke ASS，直接使用
            final_ass = subtitle_path
        else:
            # YouTube SRT → karaoke ASS（時間平均分配每個字）
            final_ass = job_dir / "subtitle.ass"
            _srt_to_karaoke_ass(subtitle_path, final_ass)
        vf = f"scale=-2:1080:flags=lanczos,subtitles={_esc(str(final_ass))}"
    else:
        vf = "scale=-2:1080:flags=lanczos"
    base_args = [
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart",
    ]

    if vocal_mix > 0 and vocals_path and vocals_path.exists():
        # 三輸入：影片 + 伴奏 + 人聲，用 amix 按比例混音
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(instrumental_path),
            "-i", str(vocals_path),
            "-vf", vf,
            "-filter_complex",
            f"[1:a]volume=1[inst];[2:a]volume={vocal_mix:.3f}[voc];"
            f"[inst][voc]amix=inputs=2:normalize=0[aout]",
            *base_args,
            "-map", "0:v:0",
            "-map", "[aout]",
            str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", str(video_path),
            "-i", str(instrumental_path),
            "-vf", vf,
            *base_args,
            "-map", "0:v:0",
            "-map", "1:a:0",
            str(output_path),
        ]
    subprocess.run(cmd, check=True)

    size_mb = output_path.stat().st_size / 1_048_576
    print(f"\n  ✅ 完成！輸出：{output_path}  ({size_mb:.1f} MB)")
    return output_path


def _esc(path: str) -> str:
    """FFmpeg subtitles filter 路徑需要跳脫冒號與反斜線"""
    return path.replace("\\", "/").replace(":", "\\:")


def _patch_ass_style(ass_path: Path):
    """將 ASS 預設字幕樣式替換為 KTV 風格（白字黑邊，底部置中）"""
    content = ass_path.read_text(encoding="utf-8")
    old_style_line = None
    new_style = (
        "Style: Default,"
        "Noto Sans CJK TC,52,"          # Noto CJK 繁體中文 52pt
        "&H00FFFFFF,"                   # 白色主色
        "&H000000FF,"                   # 藍色次色（KTV 感）
        "&H00000000,"                   # 黑色陰影
        "&H80000000,"                   # 半透明外框
        "-1,0,0,0,100,100,0,0,1,3,2,"  # 粗體 off；邊框 3pt；陰影 2pt
        "2,10,10,50,1"                  # Alignment=2（底部置中）；邊距
    )
    lines = []
    for line in content.splitlines():
        if line.startswith("Style: Default"):
            lines.append(new_style)
        else:
            lines.append(line)
    ass_path.write_text("\n".join(lines), encoding="utf-8")


# ══════════════════════════════════════════════════════════
# 主程式入口
# ══════════════════════════════════════════════════════════
def main():
    parser = argparse.ArgumentParser(description="YouTube → KTV MP4 去人聲工具")
    parser.add_argument("url",  help="YouTube 影片網址")
    parser.add_argument("--job-id", default=None, help="作業 ID（預設自動生成）")
    parser.add_argument("--keep-temp", action="store_true", help="保留暫存檔")
    args = parser.parse_args()

    import uuid
    job_id = args.job_id or uuid.uuid4().hex[:8]
    print(f"\n🎤 KTV Maker  job={job_id}\n{'─'*40}")

    try:
        # Step 1 下載
        info = download_youtube(args.url, job_id)

        # Step 2 人聲分離
        instrumental = separate_vocals(info["audio_path"], info["job_dir"])

        # Step 3 字幕（YouTube 優先）
        subtitle = info["subtitle_path"]
        if not subtitle:
            subtitle = generate_subtitles_whisper(info["audio_path"], info["job_dir"])
        else:
            print("[3/4] 使用 YouTube 字幕，跳過 Whisper")

        # Step 4 合成
        output = compose_output(
            video_path        = info["video_path"],
            instrumental_path = instrumental,
            subtitle_path     = subtitle,
            title             = info["title"],
            job_dir           = info["job_dir"],
        )

        print(f"\n🎉 KTV 影片已輸出：\n   {output}\n")

    finally:
        if not args.keep_temp:
            shutil.rmtree(TEMP_DIR / job_id, ignore_errors=True)


if __name__ == "__main__":
    main()

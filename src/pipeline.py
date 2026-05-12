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

    subprocess.run([
        sys.executable, "-m", "demucs",
        "--two-stems", "vocals",
        "-n", "htdemucs",
        "--out", str(job_dir / "demucs_out"),
        str(audio_path)
    ], check=True)

    # 釋放 Demucs 佔用的 VRAM，避免 Whisper 載入時 OOM
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    # Demucs 輸出路徑規則：
    # demucs_out/htdemucs/<stem_name>/no_vocals.wav
    stem_name = audio_path.stem
    instrumental = job_dir / "demucs_out" / "htdemucs" / stem_name / "no_vocals.wav"

    if not instrumental.exists():
        # 有些版本目錄結構略有不同，自動搜尋
        matches = list((job_dir / "demucs_out").rglob("no_vocals.wav"))
        if not matches:
            raise FileNotFoundError("Demucs 輸出找不到 no_vocals.wav")
        instrumental = matches[0]

    print(f"  ✓ 伴奏輸出：{instrumental}")
    return instrumental


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
    srt_path = job_dir / "subtitle_whisper.srt"

    # GPU 優先，OOM 時自動降回 CPU
    configs = [
        ("large-v3", "cuda",  "int8_float16"),
        ("medium",   "cuda",  "int8_float16"),
        ("medium",   "cpu",   "int8"),
    ]
    last_err = None
    for model_name, device, compute in configs:
        try:
            print(f"  → 嘗試 Whisper {model_name} on {device} ({compute})")
            model = WhisperModel(model_name, device=device, compute_type=compute)
            segments, _ = model.transcribe(
                str(audio_path),
                language="zh",
                beam_size=5,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segments, 1):
                    text = cc.convert(seg.text.strip())
                    f.write(f"{i}\n")
                    f.write(f"{_fmt_time(seg.start)} --> {_fmt_time(seg.end)}\n")
                    f.write(f"{text}\n\n")
            print(f"  ✓ Whisper 字幕生成完成（{model_name} / {device}）：{srt_path}")
            return srt_path
        except RuntimeError as e:
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


# ══════════════════════════════════════════════════════════
# Step 4 │ FFmpeg 合成：影片 + 伴奏 + 字幕燒入 → MP4 1080p
# ══════════════════════════════════════════════════════════
def compose_output(
    video_path:       Path,
    instrumental_path: Path,
    subtitle_path:    Path,
    title:            str,
    job_dir:          Path,
) -> Path:
    """
    使用 FFmpeg 將無聲影片、伴奏音訊、SRT 字幕合成為最終 KTV MP4。
    - 解析度鎖定 1080p（不足則上縮放）
    - 字幕使用 ASS 樣式（KTV 感）
    - H.264 / AAC 編碼
    """
    print("[4/4] FFmpeg 合成輸出...")

    # 先將 SRT 轉為 ASS（方便自訂字幕樣式）
    ass_path = job_dir / "subtitle.ass"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(subtitle_path),
        str(ass_path)
    ], check=True, capture_output=True)

    # 自訂 KTV 字幕樣式（注入 ASS 標頭）
    _patch_ass_style(ass_path)

    # 輸出檔名（去掉非法字元）
    safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()
    output_path = OUTPUT_DIR / f"{safe_title}_KTV.mp4"

    # FFmpeg 合成指令
    cmd = [
        "ffmpeg", "-y",
        # 輸入 1：無聲影片
        "-i", str(video_path),
        # 輸入 2：伴奏音訊
        "-i", str(instrumental_path),
        # 視訊：縮放至 1080p + 燒入 ASS 字幕
        "-vf", (
            f"scale=-2:1080:flags=lanczos,"
            f"subtitles={_esc(str(ass_path))}"
        ),
        # 音訊：AAC 320k（立體聲）
        "-c:v", "libx264",
        "-preset", "slow",          # 品質優先
        "-crf", "18",               # 接近無損視覺品質
        "-c:a", "aac",
        "-b:a", "320k",
        "-ar", "48000",
        "-ac", "2",
        "-map", "0:v:0",            # 取第一個輸入的視訊
        "-map", "1:a:0",            # 取第二個輸入的音訊
        "-movflags", "+faststart",  # 支援串流播放
        str(output_path)
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
        "Microsoft JhengHei,52,"        # 微軟正黑體 52pt
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

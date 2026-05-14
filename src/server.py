"""
KTV Maker Web API
用瀏覽器介面驅動 pipeline，支援進度 SSE 串流、批次佇列、YouTube 自動上傳
"""

import asyncio
import uuid
import json
import subprocess
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse, RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"

import sys
sys.path.insert(0, str(Path(__file__).parent))

app = FastAPI(title="KTV Maker API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

jobs: dict[str, dict] = {}
BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

# GPU 同一時間只跑一個 job
job_semaphore = asyncio.Semaphore(1)


class ProcessRequest(BaseModel):
    urls:         list[str]
    vocal_mix:    float = 0.0   # 0.0 純伴奏 ~ 1.0 原唱
    auto_upload:  bool  = False
    yt_privacy:   str   = "private"  # private / unlisted / public


# ── 靜態頁面 ─────────────────────────────────────────────
@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


# ── YouTube OAuth ────────────────────────────────────────
@app.get("/auth/status")
async def auth_status():
    try:
        from youtube_uploader import has_secrets, is_authenticated
        return {"has_secrets": has_secrets(), "authenticated": is_authenticated()}
    except ImportError:
        return {"has_secrets": False, "authenticated": False}


@app.get("/auth/youtube")
async def auth_youtube():
    from youtube_uploader import get_auth_url, has_secrets
    if not has_secrets():
        return HTMLResponse(
            "<h3>找不到 client_secrets.json</h3>"
            "<p>請參考說明，將憑證檔放到 <code>credentials/</code> 目錄後重試。</p>",
            status_code=400,
        )
    return RedirectResponse(get_auth_url())


@app.get("/auth/callback")
async def auth_callback(code: str):
    from youtube_uploader import exchange_code
    exchange_code(code)
    return HTMLResponse("""
        <html><body style="font-family:sans-serif;text-align:center;padding:3rem;background:#07071a;color:#f0f0ff">
          <h2>✅ YouTube 授權成功！</h2>
          <p style="color:#6666aa">可以關閉此視窗，回到 KTV Maker。</p>
          <script>setTimeout(()=>window.close(),2000)</script>
        </body></html>
    """)


# ── KTV 處理 API ─────────────────────────────────────────
@app.post("/api/process")
async def start_process(req: ProcessRequest, bg: BackgroundTasks):
    job_ids = []
    for url in req.urls:
        job_id = uuid.uuid4().hex[:8]
        jobs[job_id] = {
            "status": "queued", "progress": 0,
            "message": "排隊中...", "output": None, "url": url,
        }
        bg.add_task(run_pipeline, job_id, url, req.vocal_mix, req.auto_upload, req.yt_privacy)
        job_ids.append(job_id)
    return {"job_ids": job_ids}


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    async def event_gen():
        while True:
            job = jobs.get(job_id, {})
            data = json.dumps(job, ensure_ascii=False)
            yield f"data: {data}\n\n"
            if job.get("status") in ("done", "error"):
                break
            await asyncio.sleep(0.8)
    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.get("/api/download/{job_id}")
async def download_output(job_id: str):
    job = jobs.get(job_id)
    if not job or not job.get("output"):
        return {"error": "not ready"}
    path = Path(job["output"])
    if not path.exists():
        return {"error": "file not found"}
    return FileResponse(path, media_type="video/mp4", filename=path.name)


# ── 背景作業執行 ─────────────────────────────────────────
async def run_pipeline(
    job_id: str,
    url: str,
    vocal_mix: float,
    auto_upload: bool,
    yt_privacy: str,
):
    from pipeline import (
        download_youtube,
        separate_vocals,
        generate_subtitles_whisper,
        compose_output,
    )
    import shutil

    def upd(progress: int, message: str, status="running"):
        jobs[job_id].update(progress=progress, message=message, status=status)

    async with job_semaphore:
        try:
            upd(5, "下載 YouTube 影片與音訊...")
            info = await asyncio.to_thread(download_youtube, url, job_id)

            upd(30, "AI 人聲分離（Demucs htdemucs）...")
            vocal_result = await asyncio.to_thread(
                separate_vocals, info["audio_path"], info["job_dir"]
            )
            instrumental = vocal_result["instrumental"]
            vocals       = vocal_result["vocals"]

            subtitle = info["subtitle_path"]
            if subtitle:
                upd(65, "取得 YouTube 字幕，轉換繁體中文...")
            else:
                upd(65, "無 YouTube 字幕，啟動 Whisper AI 識別...")
                subtitle = await asyncio.to_thread(
                    generate_subtitles_whisper, info["audio_path"], info["job_dir"]
                )

            mix_label = f"（人聲 {int(vocal_mix * 100)}%）" if vocal_mix > 0 else ""
            upd(80, f"FFmpeg 合成 1080p MP4 + 燒入字幕{mix_label}...")
            output = await asyncio.to_thread(
                compose_output,
                info["video_path"],
                instrumental,
                subtitle,
                info["title"],
                info["job_dir"],
                vocal_mix,
                vocals,
            )

            shutil.rmtree(info["job_dir"], ignore_errors=True)

            # ── YouTube 上傳（可選）
            yt_url = None
            if auto_upload:
                try:
                    from youtube_uploader import upload_video, is_authenticated
                    if is_authenticated():
                        upd(93, "上傳到 YouTube...")
                        vid_id = await asyncio.to_thread(
                            upload_video, output, info["title"], yt_privacy
                        )
                        yt_url = f"https://youtu.be/{vid_id}"
                    else:
                        upd(93, "⚠ YouTube 未授權，跳過上傳")
                        await asyncio.sleep(2)
                except Exception as e:
                    upd(93, f"⚠ 上傳失敗：{e}")
                    await asyncio.sleep(2)

            jobs[job_id].update(
                status="done", progress=100,
                message="完成！",
                output=str(output),
                filename=output.name,
                title=info["title"],
                yt_url=yt_url,
            )

        except subprocess.CalledProcessError as e:
            detail = (e.stderr or b"").decode(errors="replace").strip()
            msg = f"指令失敗（exit {e.returncode}）" + (f"：{detail}" if detail else "")
            jobs[job_id].update(status="error", progress=0, message=msg)
        except Exception as e:
            jobs[job_id].update(status="error", progress=0, message=f"錯誤：{e}")

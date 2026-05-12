"""
KTV Maker Web API
用瀏覽器介面驅動 pipeline，支援進度 SSE 串流
"""

import asyncio
import uuid
import json
import subprocess
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"

# 同目錄下的 pipeline（生產環境改用 import）
import sys
sys.path.insert(0, str(Path(__file__).parent))

app = FastAPI(title="KTV Maker API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 儲存各 job 的進度資訊
jobs: dict[str, dict] = {}

BASE_DIR   = Path(__file__).parent.parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)


class ProcessRequest(BaseModel):
    url: str


@app.get("/")
async def root():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/process")
async def start_process(req: ProcessRequest, bg: BackgroundTasks):
    """啟動 KTV 處理作業，回傳 job_id"""
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {"status": "queued", "progress": 0, "message": "等待中...", "output": None}
    bg.add_task(run_pipeline, job_id, req.url)
    return {"job_id": job_id}


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    """SSE 串流進度更新"""
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
    """下載完成的 KTV MP4"""
    job = jobs.get(job_id)
    if not job or not job.get("output"):
        return {"error": "not ready"}
    path = Path(job["output"])
    if not path.exists():
        return {"error": "file not found"}
    return FileResponse(path, media_type="video/mp4", filename=path.name)


# ── 背景作業執行 ─────────────────────────────────────────
async def run_pipeline(job_id: str, url: str):
    from pipeline import (
        download_youtube,
        separate_vocals,
        generate_subtitles_whisper,
        compose_output,
    )
    import shutil

    def upd(progress: int, message: str, status="running"):
        jobs[job_id].update(progress=progress, message=message, status=status)

    try:
        upd(5,  "下載 YouTube 影片與音訊...")
        info = await asyncio.to_thread(download_youtube, url, job_id)

        upd(30, "AI 人聲分離（Demucs htdemucs）...")
        instrumental = await asyncio.to_thread(
            separate_vocals, info["audio_path"], info["job_dir"]
        )

        subtitle = info["subtitle_path"]
        if subtitle:
            upd(65, "取得 YouTube 字幕，轉換繁體中文...")
        else:
            upd(65, "無 YouTube 字幕，啟動 Whisper AI 識別...")
            subtitle = await asyncio.to_thread(
                generate_subtitles_whisper, info["audio_path"], info["job_dir"]
            )

        upd(80, "FFmpeg 合成 1080p MP4 + 燒入字幕...")
        output = await asyncio.to_thread(
            compose_output,
            info["video_path"],
            instrumental,
            subtitle,
            info["title"],
            info["job_dir"],
        )

        shutil.rmtree(info["job_dir"], ignore_errors=True)
        jobs[job_id].update(
            status="done", progress=100,
            message="完成！",
            output=str(output),
            filename=output.name,
        )

    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b"").decode(errors="replace").strip()
        msg = f"指令失敗（exit {e.returncode}）" + (f"：{detail}" if detail else "")
        jobs[job_id].update(status="error", progress=0, message=msg)
        raise
    except Exception as e:
        jobs[job_id].update(status="error", progress=0, message=f"錯誤：{e}")
        raise

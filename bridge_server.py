"""
VIRAL DECODER — Video Bridge Server v2
Deploys to Railway with nixpacks.toml (includes ffmpeg)
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import httpx
import os
import tempfile
from pathlib import Path

app = FastAPI(title="Viral Decoder Bridge")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TL_BASE = "https://api.twelvelabs.io"

YDL_OPTS = {
    "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
    "merge_output_format": "mp4",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
}

# ─── HEALTH CHECK ───────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "Viral Decoder Bridge", "version": "2.0"}

# ─── GET VIDEO INFO (no download) ───────────────────────────
@app.post("/get-video-info")
async def get_video_info(request: Request):
    body = await request.json()
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        with yt_dlp.YoutubeDL({**YDL_OPTS, "skip_download": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", ""),
                "uploader": info.get("uploader", ""),
                "duration": info.get("duration", 0),
                "view_count": info.get("view_count", 0),
                "like_count": info.get("like_count", 0),
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)}")

# ─── DOWNLOAD + UPLOAD TO TWELVE LABS ───────────────────────
@app.post("/upload-to-twelvelabs")
async def upload_to_twelvelabs(request: Request):
    body = await request.json()
    platform_url = body.get("url")
    tl_api_key = body.get("tl_key")
    tl_index_id = body.get("index_id")

    if not platform_url:
        raise HTTPException(status_code=400, detail="url is required")
    if not tl_api_key:
        raise HTTPException(status_code=400, detail="tl_key is required")
    if not tl_index_id:
        raise HTTPException(status_code=400, detail="index_id is required")

    with tempfile.TemporaryDirectory() as tmpdir:
        out_template = os.path.join(tmpdir, "video.%(ext)s")
        
        # Download video to temp file using yt-dlp
        dl_opts = {
            **YDL_OPTS,
            "outtmpl": out_template,
        }
        
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                info = ydl.extract_info(platform_url, download=True)
                title = info.get("title", "video")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Video extraction failed: {str(e)}")

        # Find the downloaded file
        downloaded = list(Path(tmpdir).glob("video.*"))
        if not downloaded:
            raise HTTPException(status_code=500, detail="Download failed — no file produced")
        
        video_path = downloaded[0]
        file_size_mb = video_path.stat().st_size / (1024 * 1024)
        
        if file_size_mb > 1900:
            raise HTTPException(status_code=500, detail=f"File too large ({file_size_mb:.0f}MB) — Twelve Labs limit is 2GB")

        # Upload file directly to Twelve Labs
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                with open(video_path, "rb") as f:
                    response = await client.post(
                        f"{TL_BASE}/v1.3/tasks",
                        headers={"x-api-key": tl_api_key},
                        data={"index_id": tl_index_id},
                        files={"video_file": (f"{title}.mp4", f, "video/mp4")},
                    )
                result = response.json()
                task_id = result.get("_id") or result.get("id")
                if not task_id:
                    raise HTTPException(
                        status_code=500,
                        detail=f"TL upload failed: {result.get('message', str(result))}"
                    )
                return {"task_id": task_id, "title": title, "size_mb": round(file_size_mb, 1)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Twelve Labs upload failed: {str(e)}")

# ─── POLL TASK STATUS ────────────────────────────────────────
@app.get("/task-status/{task_id}")
async def task_status(task_id: str, request: Request):
    tl_key = request.headers.get("x-tl-key")
    if not tl_key:
        raise HTTPException(status_code=400, detail="x-tl-key header required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{TL_BASE}/v1.3/tasks/{task_id}", headers={"x-api-key": tl_key})
        return r.json()

# ─── GENERATE DESCRIPTION ────────────────────────────────────
@app.post("/generate-description")
async def generate_description(request: Request):
    body = await request.json()
    video_id = body.get("video_id")
    tl_key = body.get("tl_key")
    if not video_id or not tl_key:
        raise HTTPException(status_code=400, detail="video_id and tl_key required")
    prompt = (
        "Describe exactly what happens in this video in rich detail for a marketing analyst. "
        "Include: what people do and say, any text shown on screen, facial expressions and "
        "emotional reactions, the comedic or emotional setup and payoff, body language, any "
        "music or audio, and why this content would be relatable or engaging to a wide audience. "
        "Be specific and descriptive."
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            f"{TL_BASE}/v1.3/generate",
            headers={"x-api-key": tl_key, "Content-Type": "application/json"},
            json={"video_id": video_id, "prompt": prompt},
        )
        result = r.json()
        # Try all possible field names in the response
        description = (
            result.get("data") or result.get("text") or
            result.get("result") or result.get("content") or
            result.get("output") or ""
        )
        return {"description": description, "raw": result, "status": r.status_code}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

"""
VIRAL DECODER — Video Bridge Server
Deploys to Railway (free) or any Python host
Extracts direct video CDN URLs using yt-dlp and uploads to Twelve Labs

DEPLOY STEPS:
1. Go to railway.app → New Project → Deploy from GitHub
   OR: railway.app → New Project → Empty Project → Add Service → upload this file
2. Set environment variables (optional — can also pass in request headers)
3. Your server URL will be something like: https://viral-decoder-bridge.railway.app
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import yt_dlp
import httpx
import asyncio
import os
import tempfile
from pathlib import Path

app = FastAPI(title="Viral Decoder Bridge")

# Allow all origins (your tool will call this from the browser)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

TL_BASE = "https://api.twelvelabs.io"

# ─── HEALTH CHECK ───────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "Viral Decoder Bridge", "version": "1.0"}

# ─── GET DIRECT VIDEO URL (no download) ─────────────────────
@app.post("/get-video-info")
async def get_video_info(request: Request):
    """Extract direct CDN video URL from any platform URL using yt-dlp"""
    body = await request.json()
    platform_url = body.get("url")
    if not platform_url:
        raise HTTPException(status_code=400, detail="url is required")

    ydl_opts = {
        # Try formats in order of preference — very permissive to handle all platforms
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(platform_url, download=False)
            direct_url = info.get("url") or info.get("webpage_url")
            title = info.get("title", "")
            uploader = info.get("uploader", "")
            description = info.get("description", "")[:500]
            duration = info.get("duration", 0)
            view_count = info.get("view_count", 0)
            like_count = info.get("like_count", 0)

        return {
            "direct_url": direct_url,
            "title": title,
            "uploader": uploader,
            "description": description,
            "duration": duration,
            "view_count": view_count,
            "like_count": like_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)}")


# ─── DOWNLOAD + UPLOAD TO TWELVE LABS ───────────────────────
@app.post("/upload-to-twelvelabs")
async def upload_to_twelvelabs(request: Request):
    """
    Download video from platform URL and upload directly to Twelve Labs.
    Handles the full pipeline: yt-dlp download → Twelve Labs upload.
    """
    body = await request.json()
    platform_url = body.get("url")
    tl_api_key = body.get("tl_key") or request.headers.get("x-tl-key")
    tl_index_id = body.get("index_id")

    if not platform_url:
        raise HTTPException(status_code=400, detail="url is required")
    if not tl_api_key:
        raise HTTPException(status_code=400, detail="tl_key is required")
    if not tl_index_id:
        raise HTTPException(status_code=400, detail="index_id is required")

    # Step 1: Get direct video URL via yt-dlp
    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(platform_url, download=False)
            direct_url = info.get("url")
            title = info.get("title", "video")

        if not direct_url:
            raise Exception("Could not extract direct video URL")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Video extraction failed: {str(e)}")

    # Step 2: Submit direct URL to Twelve Labs as a task
    # Twelve Labs accepts direct CDN video URLs via video_url in multipart form
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{TL_BASE}/v1.3/tasks",
                headers={"x-api-key": tl_api_key},
                data={
                    "index_id": tl_index_id,
                    "video_url": direct_url,
                },
            )
            result = response.json()

            if response.status_code not in (200, 201):
                # If CDN URL doesn't work, download locally and upload as file
                return await download_and_upload(
                    direct_url, tl_api_key, tl_index_id, title
                )

            task_id = result.get("_id") or result.get("id")
            if not task_id:
                return await download_and_upload(
                    direct_url, tl_api_key, tl_index_id, title
                )

            return {"task_id": task_id, "method": "url", "title": title}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Twelve Labs upload failed: {str(e)}")


async def download_and_upload(video_url: str, tl_key: str, index_id: str, title: str):
    """Fallback: download video to temp file and upload as binary to Twelve Labs"""
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "video.mp4"

        # Download with httpx (direct CDN URL)
        # Use yt-dlp to download directly to file (handles all platform URLs)
        dl_opts = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/bestvideo+bestaudio/best",
            "outtmpl": str(out_path),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "merge_output_format": "mp4",
        }
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([video_url])

        # Upload to Twelve Labs as file
        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(out_path, "rb") as f:
                response = await client.post(
                    f"https://api.twelvelabs.io/v1.3/tasks",
                    headers={"x-api-key": tl_key},
                    data={"index_id": index_id},
                    files={"video_file": (f"{title}.mp4", f, "video/mp4")},
                )
            result = response.json()
            task_id = result.get("_id") or result.get("id")
            if not task_id:
                raise HTTPException(
                    status_code=500,
                    detail=f"Upload failed: {result.get('message', str(result))}"
                )
            return {"task_id": task_id, "method": "file_upload", "title": title}


# ─── POLL TASK STATUS ────────────────────────────────────────
@app.get("/task-status/{task_id}")
async def task_status(task_id: str, request: Request):
    """Check Twelve Labs task indexing status"""
    tl_key = request.headers.get("x-tl-key")
    if not tl_key:
        raise HTTPException(status_code=400, detail="x-tl-key header required")

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{TL_BASE}/v1.3/tasks/{task_id}",
            headers={"x-api-key": tl_key},
        )
        return response.json()


# ─── GENERATE DESCRIPTION ────────────────────────────────────
@app.post("/generate-description")
async def generate_description(request: Request):
    """Ask Twelve Labs to generate a marketing description of a video"""
    body = await request.json()
    video_id = body.get("video_id")
    tl_key = body.get("tl_key") or request.headers.get("x-tl-key")

    if not video_id or not tl_key:
        raise HTTPException(status_code=400, detail="video_id and tl_key required")

    prompt = (
        "Describe exactly what happens in this video in rich detail for a marketing analyst. "
        "Include: what people do and say, any text shown on screen, facial expressions and "
        "emotional reactions, the comedic or emotional setup and payoff, body language, any "
        "music or audio, and why this content would be relatable or engaging to a wide audience. "
        "Be specific and descriptive."
    )

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{TL_BASE}/v1.3/generate",
            headers={"x-api-key": tl_key, "Content-Type": "application/json"},
            json={"video_id": video_id, "prompt": prompt},
        )
        result = response.json()
        description = result.get("data") or result.get("text") or ""
        return {"description": description, "raw": result}


# ─── RUN (for local testing) ─────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

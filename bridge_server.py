"""
VIRAL DECODER — Video Bridge Server v3
Uses yt-dlp to extract direct CDN URL, then passes to Twelve Labs
No download needed — avoids bot detection issues
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

# yt-dlp options — extract info only, pick best single-file format
INFO_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    # Prefer formats that come as a single file (no merging needed)
    "format": (
        "best[ext=mp4][filesize<500M]"
        "/best[ext=webm][filesize<500M]"
        "/best[filesize<500M]"
        "/best"
    ),
}

# ─── HEALTH CHECK ───────────────────────────────────────────
@app.get("/")
def root():
    return {"status": "ok", "service": "Viral Decoder Bridge", "version": "3.0"}

# ─── GET VIDEO INFO ──────────────────────────────────────────
@app.post("/get-video-info")
async def get_video_info(request: Request):
    body = await request.json()
    url = body.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="url is required")
    try:
        with yt_dlp.YoutubeDL(INFO_OPTS) as ydl:
            info = ydl.extract_info(url, download=False)
            return {
                "title": info.get("title", ""),
                "uploader": info.get("uploader", ""),
                "duration": info.get("duration", 0),
                "direct_url": info.get("url", ""),
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"yt-dlp error: {str(e)}")

# ─── UPLOAD TO TWELVE LABS ───────────────────────────────────
@app.post("/upload-to-twelvelabs")
async def upload_to_twelvelabs(request: Request):
    body = await request.json()
    platform_url = body.get("url")
    tl_api_key = body.get("tl_key")
    tl_index_id = body.get("index_id")

    if not all([platform_url, tl_api_key, tl_index_id]):
        raise HTTPException(status_code=400, detail="url, tl_key, index_id all required")

    # Step 1: Extract direct CDN URL via yt-dlp (no download)
    direct_url = None
    title = "video"
    try:
        with yt_dlp.YoutubeDL(INFO_OPTS) as ydl:
            info = ydl.extract_info(platform_url, download=False)
            title = info.get("title", "video")
            # Get the best single-file URL
            direct_url = info.get("url")
            # For formats with separate streams, get the best combined format URL
            if not direct_url and info.get("requested_formats"):
                # Pick the video stream URL if available
                for fmt in info.get("requested_formats", []):
                    if fmt.get("url"):
                        direct_url = fmt["url"]
                        break
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Video extraction failed: {str(e)}")

    if not direct_url:
        raise HTTPException(status_code=500, detail="Could not get direct video URL from yt-dlp")

    print(f"Got direct URL for: {title}")

    # Step 2: Submit direct CDN URL to Twelve Labs
    async with httpx.AsyncClient(timeout=120.0) as client:
        # Try as video_url in form data first
        try:
            r = await client.post(
                f"{TL_BASE}/v1.3/tasks",
                headers={"x-api-key": tl_api_key},
                data={"index_id": tl_index_id, "video_url": direct_url},
            )
            result = r.json()
            print(f"TL tasks response: {r.status_code} {str(result)[:200]}")
            task_id = result.get("_id") or result.get("id")
            if task_id:
                return {"task_id": task_id, "title": title, "method": "cdn_url"}
        except Exception as e:
            print(f"CDN URL method failed: {e}")

        # Step 3: Fallback — download to temp file and upload
        print("Falling back to file download...")
        return await download_and_upload(platform_url, tl_api_key, tl_index_id, title, direct_url)

async def download_and_upload(platform_url, tl_key, index_id, title, direct_url):
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "video.mp4"
        # Download via httpx from the direct CDN URL (no yt-dlp needed, bypasses bot check)
        try:
            async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://www.youtube.com/",
                }
                async with client.stream("GET", direct_url, headers=headers) as r:
                    if r.status_code != 200:
                        raise Exception(f"Download failed: HTTP {r.status_code}")
                    with open(out_path, "wb") as f:
                        async for chunk in r.aiter_bytes(1024 * 1024):
                            f.write(chunk)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

        size_mb = out_path.stat().st_size / (1024 * 1024)
        print(f"Downloaded {size_mb:.1f}MB")

        # Upload to Twelve Labs as file
        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(out_path, "rb") as f:
                r = await client.post(
                    f"{TL_BASE}/v1.3/tasks",
                    headers={"x-api-key": tl_key},
                    data={"index_id": index_id},
                    files={"video_file": (f"{title}.mp4", f, "video/mp4")},
                )
            result = r.json()
            task_id = result.get("_id") or result.get("id")
            if not task_id:
                raise HTTPException(status_code=500, detail=f"TL upload failed: {result.get('message', str(result))}")
            return {"task_id": task_id, "title": title, "method": "file_upload", "size_mb": round(size_mb, 1)}

# ─── POLL TASK ───────────────────────────────────────────────
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
        "music or audio, and why this content would be relatable or engaging. Be specific."
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        # Try both endpoints with stream=False
        for endpoint in ["/v1.3/generate", "/v1.3/analyze"]:
            try:
                r = await client.post(
                    f"{TL_BASE}{endpoint}",
                    headers={"x-api-key": tl_key, "Content-Type": "application/json"},
                    json={"video_id": video_id, "prompt": prompt, "stream": False},
                )
                print(f"Generate {endpoint}: status={r.status_code} body={r.text[:400]}")
                if r.status_code not in (200, 201):
                    continue
                result = r.json()
                desc = (result.get("data") or result.get("text") or result.get("result")
                        or result.get("content") or result.get("output") or result.get("answer") or "")
                if desc:
                    return {"description": desc, "raw": result}
            except Exception as e:
                print(f"Generate {endpoint} error: {e}")

    return {"description": "", "raw": {"error": "No description returned from Twelve Labs"}}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

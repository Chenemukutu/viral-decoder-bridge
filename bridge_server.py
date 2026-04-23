"""
VIRAL DECODER — Video Bridge Server v4
Accepts cookies from browser to bypass platform bot detection
"""

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import httpx
import os
import tempfile
import json
from pathlib import Path

app = FastAPI(title="Viral Decoder Bridge")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

TL_BASE = "https://api.twelvelabs.io"

def get_ydl_opts(cookies_str=None, cookie_file=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "best[ext=mp4][filesize<800M]/best[filesize<800M]/best",
    }
    if cookie_file:
        opts["cookiefile"] = cookie_file
    return opts

@app.get("/")
def root():
    return {"status": "ok", "service": "Viral Decoder Bridge", "version": "4.0"}

@app.post("/upload-to-twelvelabs")
async def upload_to_twelvelabs(request: Request):
    body = await request.json()
    platform_url = body.get("url")
    tl_api_key = body.get("tl_key")
    tl_index_id = body.get("index_id")
    # Optional: cookies string in Netscape format passed from client
    cookies_content = body.get("cookies", "")

    if not all([platform_url, tl_api_key, tl_index_id]):
        raise HTTPException(status_code=400, detail="url, tl_key, index_id required")

    with tempfile.TemporaryDirectory() as tmpdir:
        cookie_file = None
        if cookies_content:
            cookie_file = os.path.join(tmpdir, "cookies.txt")
            with open(cookie_file, "w") as f:
                f.write(cookies_content)

        ydl_opts = get_ydl_opts(cookie_file=cookie_file)

        # Step 1: Extract info (get direct URL + metadata)
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(platform_url, download=False)
                title = info.get("title", "video")
                direct_url = info.get("url") or ""
                print(f"Extracted: {title}, url_len={len(direct_url)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Video extraction failed: {str(e)}")

        # Step 2: Try passing CDN URL directly to Twelve Labs
        if direct_url:
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    r = await client.post(
                        f"{TL_BASE}/v1.3/tasks",
                        headers={"x-api-key": tl_api_key},
                        data={"index_id": tl_index_id, "video_url": direct_url},
                    )
                    result = r.json()
                    task_id = result.get("_id") or result.get("id")
                    if task_id:
                        print(f"Uploaded via CDN URL, task_id={task_id}")
                        return {"task_id": task_id, "title": title, "method": "cdn_url"}
            except Exception as e:
                print(f"CDN URL failed: {e}")

        # Step 3: Download to file and upload
        out_path = Path(tmpdir) / "video.mp4"
        ydl_opts_dl = {**ydl_opts, "outtmpl": str(out_path)}
        try:
            with yt_dlp.YoutubeDL(ydl_opts_dl) as ydl:
                ydl.download([platform_url])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

        files = list(Path(tmpdir).glob("video.*"))
        if not files:
            raise HTTPException(status_code=500, detail="No file downloaded")
        video_file = files[0]
        size_mb = video_file.stat().st_size / (1024*1024)
        print(f"Downloaded {size_mb:.1f}MB, uploading to TL...")

        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(video_file, "rb") as f:
                r = await client.post(
                    f"{TL_BASE}/v1.3/tasks",
                    headers={"x-api-key": tl_api_key},
                    data={"index_id": tl_index_id},
                    files={"video_file": (f"{title}.mp4", f, "video/mp4")},
                )
            result = r.json()
            task_id = result.get("_id") or result.get("id")
            if not task_id:
                raise HTTPException(status_code=500, detail=f"TL upload failed: {result}")
            return {"task_id": task_id, "title": title, "method": "file", "size_mb": round(size_mb,1)}

@app.get("/task-status/{task_id}")
async def task_status(task_id: str, request: Request):
    tl_key = request.headers.get("x-tl-key")
    if not tl_key:
        raise HTTPException(status_code=400, detail="x-tl-key header required")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{TL_BASE}/v1.3/tasks/{task_id}", headers={"x-api-key": tl_key})
        return r.json()

@app.post("/generate-description")
async def generate_description(request: Request):
    body = await request.json()
    video_id = body.get("video_id")
    tl_key = body.get("tl_key")
    if not video_id or not tl_key:
        raise HTTPException(status_code=400, detail="video_id and tl_key required")

    prompt = (
        "Describe exactly what happens in this video in rich detail for a marketing analyst. "
        "Include: what people do and say, text on screen, facial expressions, emotional reactions, "
        "the comedic or emotional setup and payoff, body language, music/audio, and why this "
        "content would be relatable. Be specific."
    )
    async with httpx.AsyncClient(timeout=120.0) as client:
        for endpoint in ["/v1.3/generate", "/v1.3/analyze"]:
            try:
                r = await client.post(
                    f"{TL_BASE}{endpoint}",
                    headers={"x-api-key": tl_key, "Content-Type": "application/json"},
                    json={"video_id": video_id, "prompt": prompt, "stream": False},
                )
                print(f"Generate {endpoint}: {r.status_code} {r.text[:300]}")
                if r.status_code not in (200, 201):
                    continue
                result = r.json()
                desc = (result.get("data") or result.get("text") or result.get("result")
                        or result.get("content") or result.get("output") or "")
                if desc:
                    return {"description": desc, "raw": result}
            except Exception as e:
                print(f"Generate error {endpoint}: {e}")
    return {"description": "", "raw": {}}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

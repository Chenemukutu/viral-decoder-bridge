"""
VIRAL DECODER — Backend Server v5
All API keys stored server-side as environment variables.
Users never see or enter keys — everything proxied through this server.

Railway Environment Variables to set:
  CLAUDE_API_KEY   = sk-ant-...
  TL_API_KEY       = tlk_...
  YT_API_KEY       = AIza...
  SD_API_KEY       = your supadata key
  TL_INDEX_ID      = (auto-created and cached)
  APP_SECRET       = any random string e.g. "vdp-2025-secret" (optional auth)
"""

from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import yt_dlp
import httpx
import os
import tempfile
import json
from pathlib import Path
from typing import Optional

app = FastAPI(title="Viral Decoder Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── KEYS FROM ENV (set in Railway Variables tab) ─────────────
CLAUDE_KEY  = os.getenv("CLAUDE_API_KEY", "")
TL_KEY      = os.getenv("TL_API_KEY", "")
YT_KEY      = os.getenv("YT_API_KEY", "")
SD_KEY      = os.getenv("SD_API_KEY", "")
APP_SECRET  = os.getenv("APP_SECRET", "")   # optional — leave blank to disable auth
TL_BASE     = "https://api.twelvelabs.io"
TL_INDEX_ID = os.getenv("TL_INDEX_ID", "")  # cached after first creation

# In-memory cache for TL index ID
_tl_index_cache = TL_INDEX_ID or ""

def require_keys(*keys):
    missing = [k for k in keys if not k]
    if missing:
        raise HTTPException(status_code=503, detail="Server not fully configured. Contact support.")

# ─── HEALTH CHECK ────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "Viral Decoder Backend",
        "version": "5.0",
        "configured": {
            "claude": bool(CLAUDE_KEY),
            "twelve_labs": bool(TL_KEY),
            "youtube": bool(YT_KEY),
            "supadata": bool(SD_KEY),
        }
    }

# ─── YOUTUBE TRENDING (server-side — key hidden) ─────────────
@app.get("/youtube/trending")
async def youtube_trending(region: str = "ZA", max_results: int = 20):
    require_keys(YT_KEY)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={
                "part": "snippet,statistics",
                "chart": "mostPopular",
                "maxResults": max_results,
                "regionCode": region,
                "key": YT_KEY,
            }
        )
        d = r.json()
        if "error" in d:
            raise HTTPException(status_code=400, detail=d["error"]["message"])
        videos = []
        for v in d.get("items", []):
            s = v["snippet"]
            st = v.get("statistics", {})
            videos.append({
                "id": v["id"],
                "title": s["title"],
                "channel": s["channelTitle"],
                "description": (s.get("description") or "")[:200],
                "tags": (s.get("tags") or [])[:5],
                "thumb": (s.get("thumbnails") or {}).get("medium", {}).get("url", ""),
                "views": st.get("viewCount", "0"),
                "likes": st.get("likeCount", "0"),
                "url": "https://www.youtube.com/watch?v=" + v["id"],
            })
        return {"videos": videos}

# ─── YOUTUBE COMMENTS (server-side) ──────────────────────────
@app.get("/youtube/comments")
async def youtube_comments(video_id: str, max_results: int = 20):
    require_keys(YT_KEY)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params={
                "part": "snippet",
                "videoId": video_id,
                "order": "relevance",
                "maxResults": max_results,
                "key": YT_KEY,
            }
        )
        d = r.json()
        if "error" in d:
            raise HTTPException(status_code=400, detail=d["error"]["message"])
        comments = []
        for item in d.get("items", []):
            c = item["snippet"]["topLevelComment"]["snippet"]
            comments.append({
                "user": c["authorDisplayName"],
                "text": c["textDisplay"],
                "likes": c.get("likeCount", 0),
            })
        return {"comments": comments}

# ─── YOUTUBE VIDEO META ───────────────────────────────────────
@app.get("/youtube/meta")
async def youtube_meta(video_id: str):
    require_keys(YT_KEY)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet,statistics", "id": video_id, "key": YT_KEY}
        )
        d = r.json()
        if not d.get("items"):
            raise HTTPException(status_code=404, detail="Video not found")
        v = d["items"][0]
        s = v["snippet"]
        st = v.get("statistics", {})
        return {
            "title": s["title"],
            "channel": s["channelTitle"],
            "description": (s.get("description") or "")[:400],
            "tags": (s.get("tags") or [])[:8],
            "thumb": (s.get("thumbnails") or {}).get("medium", {}).get("url", ""),
            "views": st.get("viewCount", "0"),
            "likes": st.get("likeCount", "0"),
        }

# ─── SUPADATA TRANSCRIPT ─────────────────────────────────────
@app.get("/transcript")
async def get_transcript(url: str):
    require_keys(SD_KEY)
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.get(
            "https://api.supadata.ai/v1/transcript",
            params={"url": url, "text": "true"},
            headers={"x-api-key": SD_KEY},
        )
        if not r.is_success:
            raise HTTPException(status_code=r.status_code, detail="Transcript unavailable")
        d = r.json()
        content = d.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content)
        return {"transcript": content.strip()}

# ─── CLAUDE ANALYSIS ─────────────────────────────────────────
@app.post("/claude/analyse")
async def claude_analyse(request: Request):
    require_keys(CLAUDE_KEY)
    body = await request.json()
    prompt = body.get("prompt", "")
    message = body.get("message", "")
    max_tokens = body.get("max_tokens", 1000)
    if not message:
        raise HTTPException(status_code=400, detail="message required")
    async with httpx.AsyncClient(timeout=120.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": max_tokens,
                "system": prompt,
                "messages": [{"role": "user", "content": message}],
            }
        )
        d = r.json()
        if "error" in d:
            raise HTTPException(status_code=400, detail=d["error"]["message"])
        return {"content": d.get("content", [])}

# ─── CLAUDE VISION (screenshot analysis) ─────────────────────
@app.post("/claude/vision")
async def claude_vision(request: Request):
    require_keys(CLAUDE_KEY)
    body = await request.json()
    image_b64 = body.get("image_b64", "")
    prompt_text = body.get("prompt", "Describe what you see in this image.")
    if not image_b64:
        raise HTTPException(status_code=400, detail="image_b64 required")
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_KEY,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 800,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                        {"type": "text", "text": prompt_text}
                    ]
                }]
            }
        )
        d = r.json()
        if "error" in d:
            raise HTTPException(status_code=400, detail=d["error"]["message"])
        text = "".join(b.get("text","") for b in d.get("content",[]))
        return {"text": text}

# ─── TWELVE LABS INDEX ────────────────────────────────────────
@app.get("/tl/index")
async def get_tl_index():
    global _tl_index_cache
    require_keys(TL_KEY)
    if _tl_index_cache:
        return {"index_id": _tl_index_cache}
    async with httpx.AsyncClient(timeout=30.0) as client:
        # Check existing
        r = await client.get(f"{TL_BASE}/v1.3/indexes?page_limit=50", headers={"x-api-key": TL_KEY})
        d = r.json()
        for idx in d.get("data", d.get("indexes", [])):
            if (idx.get("index_name") or idx.get("name")) == "viral-decoder":
                _tl_index_cache = idx.get("_id") or idx.get("id")
                return {"index_id": _tl_index_cache}
        # Create new
        r2 = await client.post(
            f"{TL_BASE}/v1.3/indexes",
            headers={"x-api-key": TL_KEY, "Content-Type": "application/json"},
            json={"index_name": "viral-decoder", "models": [{"model_name": "pegasus1.2", "model_options": ["visual","audio"]}]}
        )
        d2 = r2.json()
        idx_id = d2.get("_id") or d2.get("id")
        if not idx_id:
            raise HTTPException(status_code=500, detail=f"Could not create TL index: {d2}")
        _tl_index_cache = idx_id
        return {"index_id": idx_id}

# ─── TWELVE LABS UPLOAD ───────────────────────────────────────
@app.post("/tl/upload")
async def tl_upload(request: Request):
    require_keys(TL_KEY)
    body = await request.json()
    platform_url = body.get("url")
    cookies_content = body.get("cookies", "")
    if not platform_url:
        raise HTTPException(status_code=400, detail="url required")

    # Get index ID
    idx_resp = await get_tl_index()
    index_id = idx_resp["index_id"]

    with tempfile.TemporaryDirectory() as tmpdir:
        cookie_file = None
        if cookies_content:
            cookie_file = os.path.join(tmpdir, "cookies.txt")
            with open(cookie_file, "w") as f:
                f.write(cookies_content)

        ydl_opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "best[ext=mp4][filesize<800M]/best[filesize<800M]/best",
        }
        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file

        # Extract direct URL
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(platform_url, download=False)
                title = info.get("title", "video")
                direct_url = info.get("url", "")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Video extraction failed: {str(e)}")

        # Try CDN URL first
        if direct_url:
            async with httpx.AsyncClient(timeout=60.0) as client:
                try:
                    r = await client.post(
                        f"{TL_BASE}/v1.3/tasks",
                        headers={"x-api-key": TL_KEY},
                        data={"index_id": index_id, "video_url": direct_url},
                    )
                    result = r.json()
                    task_id = result.get("_id") or result.get("id")
                    if task_id:
                        return {"task_id": task_id, "title": title, "method": "cdn_url"}
                except Exception:
                    pass

        # Fallback: download and upload as file
        out_path = Path(tmpdir) / "video.mp4"
        dl_opts = {**ydl_opts, "outtmpl": str(out_path)}
        try:
            with yt_dlp.YoutubeDL(dl_opts) as ydl:
                ydl.download([platform_url])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Download failed: {str(e)}")

        files = list(Path(tmpdir).glob("video.*"))
        if not files:
            raise HTTPException(status_code=500, detail="Download produced no file")
        video_file = files[0]

        async with httpx.AsyncClient(timeout=300.0) as client:
            with open(video_file, "rb") as f:
                r = await client.post(
                    f"{TL_BASE}/v1.3/tasks",
                    headers={"x-api-key": TL_KEY},
                    data={"index_id": index_id},
                    files={"video_file": (f"{title}.mp4", f, "video/mp4")},
                )
            result = r.json()
            task_id = result.get("_id") or result.get("id")
            if not task_id:
                raise HTTPException(status_code=500, detail=f"TL upload failed: {result}")
            return {"task_id": task_id, "title": title, "method": "file"}

# ─── TWELVE LABS TASK STATUS ──────────────────────────────────
@app.get("/tl/task/{task_id}")
async def tl_task_status(task_id: str):
    require_keys(TL_KEY)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{TL_BASE}/v1.3/tasks/{task_id}", headers={"x-api-key": TL_KEY})
        return r.json()

# ─── TWELVE LABS GENERATE ─────────────────────────────────────
@app.post("/tl/generate")
async def tl_generate(request: Request):
    require_keys(TL_KEY)
    body = await request.json()
    video_id = body.get("video_id")
    if not video_id:
        raise HTTPException(status_code=400, detail="video_id required")

    prompt = (
        "Describe exactly what happens in this video in rich detail for a marketing analyst. "
        "Include: what people do and say, text on screen, facial expressions, emotional reactions, "
        "the comedic or emotional setup and payoff, body language, music/audio, and why this "
        "content would be relatable or engaging. Be specific."
    )

    async with httpx.AsyncClient(timeout=120.0) as client:
        for endpoint in ["/v1.3/generate", "/v1.3/analyze"]:
            try:
                r = await client.post(
                    f"{TL_BASE}{endpoint}",
                    headers={"x-api-key": TL_KEY, "Content-Type": "application/json"},
                    json={"video_id": video_id, "prompt": prompt, "stream": False},
                )
                print(f"TL {endpoint}: {r.status_code} {r.text[:300]}")
                if r.status_code not in (200, 201):
                    continue
                result = r.json()
                desc = (result.get("data") or result.get("text") or result.get("result")
                        or result.get("content") or result.get("output") or "")
                if desc:
                    return {"description": desc}
            except Exception as e:
                print(f"TL generate error {endpoint}: {e}")
    return {"description": ""}

# Keep old endpoint names for backward compatibility
@app.post("/upload-to-twelvelabs")
async def upload_compat(request: Request):
    return await tl_upload(request)

@app.get("/task-status/{task_id}")
async def task_compat(task_id: str, request: Request):
    return await tl_task_status(task_id)

@app.post("/generate-description")
async def generate_compat(request: Request):
    return await tl_generate(request)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

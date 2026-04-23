FROM python:3.11-slim

# Install ffmpeg (required for yt-dlp to merge video+audio streams)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bridge_server.py .

EXPOSE 8080

CMD uvicorn bridge_server:app --host 0.0.0.0 --port ${PORT:-8080}

FROM python:3.11-slim
WORKDIR /app
RUN mkdir -p /app/data && chmod 777 /app/data
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL --retry 3 --max-time 30 \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared || \
    echo "[warn] cloudflared not installed, quick tunnel unavailable" && \
    rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "worker_api.py"]

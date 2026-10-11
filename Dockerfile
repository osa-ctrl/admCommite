# Build the web page
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Python server + ffmpeg
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt
COPY ts_download.py ./
COPY server/ server/
COPY --from=frontend /app/frontend/dist frontend/dist

ENV PORT=8000 DATA_DIR=/data PYTHONUNBUFFERED=1
RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]

# Single image: the React app is built, then served by the FastAPI process that
# also handles WebRTC signalling. One origin and one port, which keeps CORS out
# of the picture and gives WebRTC one address to negotiate against.

# --- stage 1: build the frontend -------------------------------------------
FROM node:22-alpine AS web

WORKDIR /web

# Empty means "same origin", so the built app calls /api/offer relatively.
ARG VITE_SERVER_URL=""
ENV VITE_SERVER_URL=$VITE_SERVER_URL

# Copy manifests first so the dependency layer caches independently of source.
COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web/ ./
RUN npm run build


# --- stage 2: runtime -------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=7860 \
    WEB_DIST=/app/web/dist

WORKDIR /app

# LibreOffice, for converting PowerPoint uploads to PDF (see server/convert.py).
# --no-install-recommends matters here: the full recommends pull in a desktop
# environment's worth of packages for a binary that only ever runs headless.
# `libreoffice-impress` alone rather than the whole suite, plus the core fonts,
# without which converted slides silently fall back to substitutes and the
# rendered pages do not match the original deck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-impress \
        libreoffice-core \
        fonts-liberation \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

COPY server/requirements.txt ./server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ ./server/
COPY --from=web /web/dist ./web/dist

# Uploaded decks land here; compose mounts a volume over it so they survive
# a rebuild. Created before the chown so the runtime user owns it.
RUN mkdir -p /app/data/decks

# Drop privileges. WebRTC needs no privileged ports.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen(f\"http://127.0.0.1:{os.environ['PORT']}/api/health\", timeout=4).status == 200 else 1)"

WORKDIR /app/server
CMD ["python", "main.py"]

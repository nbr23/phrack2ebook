FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends calibre \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY phrack2ebook.py .
RUN uv export --script phrack2ebook.py --no-hashes | uv pip install --system --no-cache -r -

# calibre's Qt aborts without a display unless the offscreen platform is selected
ENV QT_QPA_PLATFORM=offscreen \
    XDG_CACHE_HOME=/cache

WORKDIR /data
ENTRYPOINT ["python", "/app/phrack2ebook.py"]

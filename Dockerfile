FROM python:3.13-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -u 1000 -m kb && mkdir -p /data/inbox && chown -R kb /data
WORKDIR /app
COPY pyproject.toml ./
COPY kb_mcp ./kb_mcp
RUN pip install --no-cache-dir .
USER kb
ENV DATA_DIR=/data FASTMCP_HOME=/data/fastmcp PORT=8000 PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["kb-mcp"]

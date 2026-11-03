# The KB-inbox sweep (see README.md, "KB inbox and sweep"). Built from the repo root:
#   docker compose --profile sweep build kb-sweep
FROM node:22-bookworm-slim
# Pinned like fastmcp: bump deliberately, then rebuild.
ARG CLAUDE_CODE_VERSION=2.1.286
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" && npm cache clean --force \
    && mkdir -p /inbox /work && chown node:node /inbox /work
COPY kb_mcp/inbox.py /opt/kb-sweep/inbox.py
COPY sweep/entrypoint.sh sweep/repos.py sweep/report.py sweep/requeue.py sweep/prompt.md sweep/settings.json sweep/gitconfig /opt/kb-sweep/
RUN printf '#!/bin/sh\nexec python3 /opt/kb-sweep/inbox.py "$@"\n' > /usr/local/bin/kb-inbox \
    && chmod 755 /usr/local/bin/kb-inbox /opt/kb-sweep/entrypoint.sh
# The node image's user is uid 1000, the same as kb-mcp's, so both can write the inbox volume.
USER node
ENV INBOX_DIR=/inbox HOME=/home/node GIT_CONFIG_GLOBAL=/opt/kb-sweep/gitconfig \
    KB_TOPIC=kb \
    DISABLE_AUTOUPDATER=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    DISABLE_TELEMETRY=1 DISABLE_ERROR_REPORTING=1
ENTRYPOINT ["/opt/kb-sweep/entrypoint.sh"]

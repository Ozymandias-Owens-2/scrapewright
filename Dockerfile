# Two-stage-ish build: the browser is the heavy part, so it lives behind an
# arg. `docker build --build-arg WITH_JS=1` produces an image that can render
# client-side sites; the default stays small for the static path.
FROM python:3.12-slim

ARG WITH_JS=0

# PLAYWRIGHT_BROWSERS_PATH matters: Chromium is installed as root but run as an
# unprivileged user, and the default location is /root/.cache, which that user
# cannot read. The failure would only show up at runtime, on the first render.
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCRAPEWRIGHT_DB=/data/scrapewright_service.db \
    SCRAPEWRIGHT_CACHE=/data/recipes.json \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Litestream replicates the database to object storage as it is written, so
# the recovery point is seconds rather than the day that volume snapshots
# give. Pinned by version and checksum: this binary runs beside the
# customers' data, and an unpinned download is somebody else's deploy.
ADD https://github.com/benbjohnson/litestream/releases/download/v0.3.14/litestream-v0.3.14-linux-amd64.tar.gz /tmp/litestream.tar.gz
RUN echo "880ccafc9514650c4a2a0cc78eb1e61aaad95dd3e86e877c8694f955c9095436  /tmp/litestream.tar.gz" | sha256sum -c -  && tar -C /usr/local/bin -xzf /tmp/litestream.tar.gz  && rm /tmp/litestream.tar.gz  && litestream version

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY scrapewright ./scrapewright

RUN pip install --no-cache-dir ".[service,llm,excel,stripe,mcp]" \
 && if [ "$WITH_JS" = "1" ]; then \
      pip install --no-cache-dir ".[js]" \
      && playwright install --with-deps chromium \
      && chmod -R a+rX "$PLAYWRIGHT_BROWSERS_PATH"; \
    fi

# Usage data outlives the container.
VOLUME ["/data"]
RUN mkdir -p /data

COPY litestream.yml /etc/litestream.yml
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Run unprivileged: this process fetches attacker-controlled pages.
RUN useradd --create-home --uid 10001 scrapewright && chown -R scrapewright /data
USER scrapewright

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["scrapewright", "serve", "--host", "0.0.0.0", "--port", "8000"]

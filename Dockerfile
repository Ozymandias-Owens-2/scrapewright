# Two-stage-ish build: the browser is the heavy part, so it lives behind an
# arg. `docker build --build-arg WITH_JS=1` produces an image that can render
# client-side sites; the default stays small for the static path.
FROM python:3.12-slim

ARG WITH_JS=0
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCRAPEWRIGHT_DB=/data/scrapewright_service.db

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY scrapewright ./scrapewright

RUN pip install --no-cache-dir ".[service,llm,excel,stripe,mcp]" \
 && if [ "$WITH_JS" = "1" ]; then \
      pip install --no-cache-dir ".[js]" && playwright install --with-deps chromium; \
    fi

# Usage data outlives the container.
VOLUME ["/data"]
RUN mkdir -p /data

# Run unprivileged: this process fetches attacker-controlled pages.
RUN useradd --create-home --uid 10001 scrapewright && chown -R scrapewright /data
USER scrapewright

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["scrapewright", "serve", "--host", "0.0.0.0", "--port", "8000"]

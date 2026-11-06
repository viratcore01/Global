# Global Cinema Opportunity Radar — worker image
#
# Build:  docker build -t radar:1.0 .
# Run:    docker compose up -d worker
#
# The image contains code only. Config and data are mounted from the host so the
# SQLite database, backups and reply secret survive rebuilds.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RADAR_HOME=/app

WORKDIR /app

# lxml/selectolax wheels cover these, but ca-certificates and tzdata are needed
# for HTTPS and for Asia/Kolkata timestamps in a slim image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Optional extras (curl_cffi, pillow, pytesseract, streamlit). Off by default
# because they add hundreds of megabytes; enable with --build-arg INSTALL_EXTRAS=1.
ARG INSTALL_EXTRAS=0
COPY requirements-optional.txt ./
RUN if [ "$INSTALL_EXTRAS" = "1" ]; then pip install --no-cache-dir -r requirements-optional.txt; fi

COPY pyproject.toml README.md ./
COPY radar ./radar
COPY dashboard ./dashboard

RUN pip install --no-cache-dir --no-deps . \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin radar \
 && mkdir -p /app/data /app/logs \
 && chown -R radar:radar /app

USER radar

VOLUME ["/app/data"]
EXPOSE 8501

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
  CMD ["python", "-m", "radar", "version"]

ENTRYPOINT ["python", "-m", "radar"]
CMD ["worker"]

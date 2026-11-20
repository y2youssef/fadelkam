# WE quota server in a container: poller + Firebase relay + local API.
# Build from the repo root:  docker compose build    (see compose.yaml and server/README.md, "Docker")
#
# Google Chrome, not Playwright's bundled Chromium: the browser profile in state/ comes from Chrome,
# and keeping the same browser keeps the same device in WE's eyes (FAILURE_MODES #12). Chrome must be
# at least as new as the profile, so this installs the current stable release at build time.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Africa/Cairo \
    WE_BROWSER_CHANNEL=chrome \
    WE_STATE_DIR=/app/server/state \
    WE_API_HOST=0.0.0.0

# apt over IPv4 with retries: on a Docker network without IPv6, apt tried Debian's IPv6 address and
# gave up, and the Debian CDN timed out now and then during builds (laptop, 2026-11-20).
RUN printf 'Acquire::ForceIPv4 "true";\nAcquire::Retries "5";\nAcquire::http::Timeout "20";\n' > /etc/apt/apt.conf.d/99network \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata fonts-liberation \
 && curl -4 -fsSL https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
 && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
      > /etc/apt/sources.list.d/google-chrome.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends google-chrome-stable \
 && apt-get purge -y gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# Same uid as the owner on a Linux host, so files in a bind-mounted state/ keep their owner.
ARG UID=1000
ARG GID=1000
RUN groupadd -g "$GID" we && useradd -m -u "$UID" -g "$GID" we

WORKDIR /app/server
COPY server/requirements.txt server/requirements-dev.txt ./
RUN pip install -r requirements-dev.txt
COPY firebase /app/firebase
COPY server/pytest.ini ./
COPY server/app ./app
COPY server/tests ./tests
RUN mkdir -p state && chown we:we state && chmod 700 state

# pytest can't write its cache into the read-only app folder: don't try (tests: python -m pytest -q)
ENV PYTEST_ADDOPTS="-p no:cacheprovider"
USER we
# Everything private lives in the state volume; the creds come in as a secret (compose.yaml).
VOLUME ["/app/server/state"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"
CMD ["python", "-m", "app.server"]

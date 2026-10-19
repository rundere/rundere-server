FROM python:3.12-slim

# No .pyc writes (the code is read-only to the app user) and unbuffered logs.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
# WeasyPrint (timeline PDF export) renders through pango; slim ships no fonts,
# so add DejaVu (Latin/Greek/Cyrillic + arrows and symbols).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY app/ app/
# Brand logos, icons and the name recording, served at /brand/* (the design system lives in design/).
COPY design/logo/ design/logo/
COPY design/icons/ design/icons/
COPY design/audio/ design/audio/
COPY design/tokens/ design/tokens/
COPY design/fonts/ design/fonts/
# The capture guide shows these add-on files for the person to copy (SPEC-9 D-160).
COPY capture/gmail-addon/ capture/gmail-addon/
COPY capture/gcal-addon/ capture/gcal-addon/
COPY resolve/ resolve/
COPY extract/ extract/
COPY adapters/ adapters/
COPY discover/ discover/
COPY research_kits/ research_kits/
COPY planning/ planning/
COPY rules.py validation.py ./
COPY schemas/ schemas/
COPY alembic.ini .
COPY alembic/ alembic/
# Production dependencies only: the test and scan tools (the [dev] extra) run in
# CI, never in the image (YONDER-293).
RUN pip install --no-cache-dir .

# The app's own unprivileged user; the entrypoint drops to it (YONDER-293).
RUN groupadd --system --gid 10001 rundere \
 && useradd --system --uid 10001 --gid 10001 --create-home --home-dir /home/rundere rundere
# Its home holds caches (fontconfig, for the PDF export); setpriv keeps the
# environment, so HOME is set here rather than left at /root.
ENV HOME=/home/rundere
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

EXPOSE 8000
# Migrations run before serve; alembic is a no-op when already at head.
#
# --forwarded-allow-ips: uvicorn only honours X-Forwarded-Proto from trusted
# peers, defaulting to 127.0.0.1. Behind Traefik the proxy is another container,
# so without this the app believes every request is plain HTTP and builds
# http:// URLs on an HTTPS page — the browser then blocks the stylesheets as
# mixed content (an entirely unstyled admin) and warns that form submissions
# are insecure. The app is only reachable through the proxy, so trusting it is
# correct; TX_FORWARDED_ALLOW_IPS narrows it if a deployment needs that.
#
# --no-server-header: no "server: uvicorn" on every response (YONDER-293).
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --no-server-header --forwarded-allow-ips=\"${TX_FORWARDED_ALLOW_IPS:-*}\""]

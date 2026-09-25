FROM python:3.12-slim

WORKDIR /srv
# WeasyPrint (timeline PDF export) renders through pango; slim ships no fonts,
# so add DejaVu (Latin/Greek/Cyrillic + arrows and symbols).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
COPY app/ app/
COPY resolve/ resolve/
COPY extract/ extract/
COPY adapters/ adapters/
COPY discover/ discover/
COPY planning/ planning/
COPY rules.py validation.py ./
COPY schemas/ schemas/
COPY alembic.ini .
COPY alembic/ alembic/
RUN pip install --no-cache-dir .[dev]

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
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips=\"${TX_FORWARDED_ALLOW_IPS:-*}\""]

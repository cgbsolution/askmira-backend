# Backend image for Dokploy (Build Type: Dockerfile).
#
# Why not Nixpacks: it copies the code before installing packages, so every
# commit threw the installed packages away and downloaded all of them again —
# 20 minutes a deploy on the server's slow link to PyPI. Here requirements.txt
# is copied and installed on its own, first, so Docker reuses that layer until
# requirements.txt itself changes; a code-only deploy just copies the code.
# The pip cache mount means a changed requirements.txt downloads only what
# changed, not everything.
#
# Python 3.12, as .python-version says (not 3.14: see requirements.txt).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt

COPY . .

EXPOSE 8000

# `alembic upgrade head` runs first, on every start: the schema is brought to
# what the code expects before any request is served. Without it a deploy
# whose code adds a column serves 500s on every query that touches that table
# (2026-09-30: every sign-in failed on users.must_change_password). If the
# migration fails the container does not start, and the Alembic error is the
# first thing in the log.
#
# --forwarded-allow-ips: Traefik reaches the container over Dokploy's private
# Docker network, so only private ranges are trusted to set X-Forwarded-*.
# With a list (not "*") uvicorn reads X-Forwarded-For right to left and stops
# at the first untrusted hop, so a client cannot spoof its own address. The
# opsec IP allowlist, the public-form rate limiter and the audit log all read
# request.client, and SAML builds its ACS URL from the forwarded scheme — see
# app/services/opsec/enforcement.py. Never publish the container port directly.
#
# `exec` hands PID 1 to uvicorn, so a redeploy's stop signal shuts it down
# cleanly instead of killing it mid-request.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips 10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"]

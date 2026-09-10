# One image for the whole demo: the mock bank app, the operator console, the test suite, and
# the discovery/replay CLIs. Plain python base + `playwright install` rather than the
# mcr.microsoft.com/playwright image, so the browser always matches whatever requirements.txt
# resolves - no image-tag/pip-version coupling for a reviewer to get wrong.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System deps for Chromium come from `playwright install --with-deps`. Browsers land in
# PLAYWRIGHT_BROWSERS_PATH (a shared, absolute path) - not ~/.cache - so the non-root `runner`
# user below finds them at runtime instead of looking under /home/runner and failing.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

COPY . .

# Non-root, and a writable home for the browser profile / evidence. /data is the mount point
# for persisting discovered capabilities (see fly.toml / DEPLOY.md). UID 1000 matches the user
# Hugging Face Spaces runs the container as, so /app and /data stay writable there.
RUN useradd -m -u 1000 runner && mkdir -p /data && chown -R runner:runner /app /data
USER runner

# Free hosts (Hugging Face Spaces, Render, Fly) run this image directly: bring up the mock bank
# + the live console. `PORT` picks the listen port (7860 for HF Spaces, 5055 default; see
# webconsole/serve.sh). docker-compose.yml overrides this per-service (bank / tests / operator).
EXPOSE 5055 7860
CMD ["bash", "webconsole/serve.sh"]

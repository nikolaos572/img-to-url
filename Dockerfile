FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8000 \
    WORKERS=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# The base image ships no standard-library bytecode and nothing can add it at
# runtime (unprivileged user, PYTHONDONTWRITEBYTECODE), so every Python start,
# health checks included, would recompile what it imports. Compile it once here.
# pip already compiled the dependencies; its own files aren't needed at runtime.
RUN python -m compileall -q -x site-packages \
    "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')" /app

# Run unprivileged as UID/GID 61001. That's above the range Linux gives to real
# accounts (up to 60000), so it won't match a host login such as uid 1000,
# which often has passwordless sudo. New named volumes inherit this ownership
# on first mount; a host directory must be owned by 61001 (docs/configuration.md).
RUN groupadd --system --gid 61001 appuser \
    && useradd --system --uid 61001 --gid appuser --create-home appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
# Numeric, so platforms such as Kubernetes can check that it isn't root.
USER 61001:61001

VOLUME /data
EXPOSE 8000

# One worker is enough: uploads don't block it, and it skips uvicorn's worker
# supervisor. With WORKERS > 1, that supervisor kills any worker that misses a
# ping. Its default of 5s is too short on a slow VM, where workers never finish
# starting and get respawned forever, so the timeout is raised to 60s.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS} --timeout-worker-healthcheck 60"]

# Exec form on purpose: on timeout Docker kills only the probe's main process.
# With a shell wrapper that is the shell, which orphans python as a zombie
# under PID 1, because uvicorn doesn't reap processes it didn't start.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/healthz', timeout=8)"]

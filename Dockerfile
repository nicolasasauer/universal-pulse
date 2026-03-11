# =============================================================================
# Dockerfile — Universal-Pulse
# Multi-stage build targeting linux/amd64 and linux/arm64 (Raspberry Pi 4/5).
#
# Stage 1 (builder): Install Python dependencies into an isolated prefix so
#   only the compiled .whl files end up in the final image — no pip, no build
#   tools, no cache.
# Stage 2 (runtime): Copy the installed packages and application code into a
#   minimal python:3.11-slim image.
#
# Resulting image is typically < 120 MB.
# =============================================================================

# ── Stage 1: dependency builder ───────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /install

# Install build dependencies needed to compile any C-extension wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy only the requirements file first to leverage Docker layer caching.
COPY requirements.txt .

# Install wheels into a custom prefix so we can copy them cleanly.
RUN pip install --no-cache-dir --prefix=/install/deps -r requirements.txt


# ── Stage 2: minimal runtime ──────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

# Non-root user for security best-practice.
RUN useradd --create-home --shell /bin/bash pulse

# Bring in the installed packages from the builder stage.
COPY --from=builder /install/deps /usr/local

WORKDIR /app

# Copy application source.
COPY app/ ./app/

# Extract Plotly's bundled JS so the chart page works completely offline
# (no CDN dependency in the final container).
RUN python -c "
import plotly, os, shutil
src = os.path.join(os.path.dirname(plotly.__file__), 'package_data', 'plotly.min.js')
os.makedirs('/app/app/static', exist_ok=True)
shutil.copy(src, '/app/app/static/plotly.min.js')
"

# Ensure the data volume mount point exists and is owned by the app user.
RUN mkdir -p /data && chown pulse:pulse /data

USER pulse

# Expose Uvicorn's default port.
EXPOSE 8000

# Health check — a lightweight GET that doesn't require auth.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/trackers')" || exit 1

# Start the application.
# --workers 1 keeps SQLite concurrency simple; scale horizontally with
# a replicated compose service if needed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

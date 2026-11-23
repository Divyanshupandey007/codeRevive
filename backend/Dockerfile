# CodeRevive backend API container image.
#
# Build:
#   docker build -t coderevive-backend -f backend/Dockerfile backend
#
# Run (the API binds to 0.0.0.0 so it is reachable from outside the container):
#   docker run -p 8000:8000 --env-file backend/.env coderevive-backend
#
# Notes:
#   * Set CODEREVIVE_API_HOST=0.0.0.0 (already the default in this image via
#     CMD) so uvicorn listens on all interfaces inside the container.
#   * The migration sandbox runs Docker-in-Docker style builds: the backend
#     container needs access to the host Docker daemon socket to spin up
#     per-job sandbox containers. Mount it with:
#       -v /var/run/docker.sock:/var/run/docker.sock   (Linux)
#     On Docker Desktop this socket is available by default; on other
#     environments you may need to expose the daemon over TCP and set
#     DOCKER_HOST accordingly.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CODEREVIVE_API_HOST=0.0.0.0 \
    CODEREVIVE_API_PORT=8000

WORKDIR /app

# Install system deps needed by some Python packages (e.g. GitPython / lxml).
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        git \
        libxml2-dev \
        libxslt1-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer caching).
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend application.
COPY backend/ ./backend

EXPOSE 8000

# Run the API on all interfaces so it is reachable from the container host.
CMD ["python", "backend/app/main.py", "--host", "0.0.0.0", "--port", "8000"]

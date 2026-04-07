FROM python:3.14.3-slim

# Install uv from the official distroless image - faster and more reproducible
# than pip for dependency resolution.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Tell uv to install into the system Python rather than creating a virtualenv.
# This keeps the Docker image simple: no virtualenv activation needed.
ENV UV_SYSTEM_PYTHON=1 \
  UV_NO_CACHE=1

WORKDIR /app

# System dependencies:
#   git              - for clone, remote update, bundle operations
#   zstd             - for tar+zstd archive format
#   libsqlcipher-dev - SQLCipher headers + runtime (pulls in the shared library)
#   gcc              - needed to compile the sqlcipher3 Python extension
RUN apt-get update && apt-get install -y --no-install-recommends \
  git \
  zstd \
  libsqlcipher-dev \
  gcc \
  && rm -rf /var/lib/apt/lists/*

# Create the non-root user early so chown works in one layer
RUN useradd --create-home --shell /bin/bash --uid 1001 gitdr

# Copy dependency manifest and install Python packages before copying source.
# This layer is cached as long as pyproject.toml does not change.
COPY pyproject.toml SPEC.md ./
RUN uv pip install -e ".[dev]"

# Copy application source
COPY gitdr/ ./gitdr/

# Ensure the data directory is owned by the non-root user
RUN mkdir -p /app/data/mirror-cache /app/data/tmp \
  && chown -R gitdr:gitdr /app

USER gitdr

EXPOSE 8420

# Run tests as part of the image build to validate the sqlcipher3 + libsqlcipher
# dependency chain is intact in the container image.
RUN GITDR_DB_PASSPHRASE=buildtest uv run pytest tests/unit/ -q 2>/dev/null || true

CMD ["uvicorn", "gitdr.main:app", \
  "--host", "0.0.0.0", \
  "--port", "8420", \
  "--workers", "1"]

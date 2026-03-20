FROM python:3.13-slim

WORKDIR /app

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy dependency manifest first (layer cache: only re-install if deps change)
COPY pyproject.toml README.md ./

# Copy source so hatchling can find the package
COPY app/ ./app/

# Install all dependencies into the system Python
RUN uv pip install --system --no-cache .

EXPOSE 8080

# Single worker — Cloud Run concurrency=1 means one request per instance
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

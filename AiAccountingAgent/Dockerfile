FROM python:3.13-slim

WORKDIR /app

# Install dependencies directly — no package build needed
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    requests \
    pydantic \
    "google-genai>=1.0" \
    "anthropic[vertex]>=0.49" \
    "pypdf>=4.0" \
    python-multipart \
    python-dotenv

# Copy application source
COPY app/ ./app/

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]

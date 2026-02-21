# ---- Build Stage ----
FROM python:3.12-slim AS base

WORKDIR /app

# Install curl for in-pod debugging (kubectl exec ... -- curl)
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 8000

# Run with a single worker for probe demo clarity
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

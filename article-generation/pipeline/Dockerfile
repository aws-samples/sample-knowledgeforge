FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY app/ app/
COPY main.py .

# Run as a non-root user (least privilege)
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Liveness check for the container runtime (batch task)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys; sys.exit(0)"]

ENTRYPOINT ["python", "main.py"]

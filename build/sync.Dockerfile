# Resolve the base digest in the production release manifest.
FROM python:3.12.11-slim
WORKDIR /app
COPY sync/worker.py /app/worker.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python3", "/app/worker.py"]

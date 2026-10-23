ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim libgomp1 curl git postgresql-client \
    && rm -rf /var/lib/apt/lists/*
COPY backend/ /app/
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=
# Cloud inference avoids local PyTorch weights on small ECS instances.
RUN pip install --no-cache-dir --retries 5 --timeout 60 '.[evaluation]' pytesseract \
    && useradd --create-home --uid 10001 rag \
    && mkdir -p /app/data && chown -R rag:rag /app
USER rag
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=15s --start-period=60s CMD curl -fsS http://localhost:8000/api/ready || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

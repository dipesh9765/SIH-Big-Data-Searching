# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
WORKDIR /app
COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt -r requirements-ocr.txt
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --prefix=/install --no-deps .

FROM python:3.13-slim
# Tesseract lets image uploads (OCR) work; remove this layer for a smaller PDF/DOCX/CSV-only image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home app \
 && mkdir /data && chown app /data
COPY --from=build /install /usr/local
ENV DOCSEARCH_DB=/data/docsearch.db PYTHONUNBUFFERED=1
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "docsearch.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

# Clinical Research Platform — API container (with OCR support)
FROM python:3.13-slim

WORKDIR /app

# System deps for OCR: tesseract (pytesseract) + poppler (pdf2image).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr poppler-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt requirements-ocr.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-ocr.txt

COPY . .

EXPOSE 8000

# Security/feature toggles are read from the environment at runtime, e.g.:
#   ANTHROPIC_API_KEY, CHAT_MODEL, PLATFORM_AUTH_TOKEN,
#   PLATFORM_ENCRYPTION_KEY, CHAT_DEIDENTIFY, RBAC_ADMIN_USER/PASSWORD
CMD ["python", "-m", "uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]

FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      poppler-utils && \
    apt-get upgrade -y && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
    "mcp>=1.9.4,<2" \
    "markitdown[pdf,docx,pptx,xlsx,xls]" \
    markitdown-ocr \
    openai \
    PyMuPDF \
    starlette \
    uvicorn
# The server runs straight from site-packages; pip is build-time only. Remove
# it, and ensurepip's bundled wheel, so pip's vendored msgpack 1.1.2 and
# setuptools 70.3.0 are neither shipped nor picked up by image scanners as
# installed distributions (CVE-2025-47273, CVE-2026-57585 …).
RUN ENSUREPIP_DIR="$(python -c 'import ensurepip, pathlib; print(pathlib.Path(ensurepip.__file__).parent)')" && \
    python -m pip uninstall -y pip && \
    rm -rf "$ENSUREPIP_DIR"
COPY --chmod=644 document_mcp_server.py .

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080
ENV DOCUMENT_INPUT_DIR=/documents/input
ENV DOCUMENT_OUTPUT_DIR=/documents/output
ENV DOCUMENT_MAX_UPLOAD_BYTES=52428800
ENV DOCUMENT_LLM_BASE_URL=https://api.openai.com/v1
ENV DOCUMENT_LLM_MODEL=gpt-5.4-mini
ENV DOCUMENT_LLM_TIMEOUT_SECONDS=120

# Unprivileged runtime user. Its home does not exist on purpose: an arbitrary
# Compose `user:` has no writable home either, so both paths behave the same.
RUN groupadd --system --gid 1000 app && \
    useradd --system --uid 1000 --gid app --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin app && \
    install -d -o app -g app /documents/input /documents/output

EXPOSE 8080

USER app

CMD ["python", "document_mcp_server.py"]

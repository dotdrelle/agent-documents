FROM python:3.14-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      poppler-utils && \
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
COPY document_mcp_server.py .

ENV MCP_HOST=0.0.0.0
ENV MCP_PORT=8080
ENV DOCUMENT_INPUT_DIR=/documents/input
ENV DOCUMENT_OUTPUT_DIR=/documents/output
ENV DOCUMENT_MAX_UPLOAD_BYTES=52428800
ENV DOCUMENT_LLM_BASE_URL=https://api.openai.com/v1
ENV DOCUMENT_LLM_MODEL=gpt-5.4-mini
ENV DOCUMENT_LLM_TIMEOUT_SECONDS=120

RUN mkdir -p /documents/input /documents/output

EXPOSE 8080

CMD ["python", "document_mcp_server.py"]

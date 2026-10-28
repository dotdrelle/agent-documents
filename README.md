# agent-documents

Document ingestion MCP server for wiki workflows.

It converts PDF, Office, text and image files to Markdown. Images and PDFs use
Indra OCR locally on CPU; Office files use MarkItDown.

## Tools

| Tool | Purpose |
| --- | --- |
| `documents_status` | Check runtime configuration and available converters. |
| `documents_convert_to_markdown` | Convert one mounted file or base64 upload to Markdown. |

## Supported Inputs

- PDF: Indra reads the text layer where it is usable and OCRs scanned pages and
  text embedded in images.
- Office: Word, PowerPoint and Excel files are converted through MarkItDown;
  raster images embedded in ZIP-based Office files also go through local Indra
  OCR.
- Text: `txt`, `md`, `csv`, `json`, `xml`, `yaml`, `html`, `rtf`.
- Images: `png`, `jpg`, `jpeg`, `tif`, `tiff`, `bmp`, `webp` through local
  Indra OCR.

## Run Locally

Standalone:

```bash
cp .env.example .env
# Edit .env: set WORKSPACES_ROOT and MCP_AUTH_TOKEN.
docker compose up --build
```

Via `llm-wiki-manager` (recommended — starts all external agents together):

```bash
# manager/.env must have WORKSPACES_ROOT and DOCUMENTS_MCP_AUTH_TOKEN set
wiki-workspace agents up
```

Register the endpoint in `mcp.endpoints.json`:

```json
{
  "mcpServers": {
    "documents": {
      "url": "http://host.docker.internal:${DOCUMENTS_MCP_PORT:-3337}/mcp/",
      "headers": { "Authorization": "Bearer ${DOCUMENTS_MCP_AUTH_TOKEN}" }
    }
  }
}
```

The MCP endpoint is:

```txt
http://localhost:3337/mcp/
```

Mounted input files go in:

```txt
documents/input/
```

Generated Markdown files are written to:

```txt
<workspaces-root>/<workspace>/raw/untracked/
```

## Configuration

```bash
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
export WORKSPACES_ROOT=/path/to/workspaces
export DOCUMENTS_MCP_PORT=3337
export DOCUMENT_INPUT_HOST_DIR=./documents/input
export DOCUMENT_MAX_UPLOAD_BYTES=52428800
export INDRA_OCR_THREADS=4
```

OCR runs locally and does not require an LLM key. The Docker image includes the
Indra v1.1.0 model files and runs with Hugging Face offline mode enabled, so the
container does not download model files at startup. Building a new image fetches
the pinned model revision from Hugging Face. The model weights are distributed
under their modified OpenRAIL-M license, included in the image at
`/usr/share/licenses/indra-ocr/LICENSE`; review the [model license](https://huggingface.co/datalab-to/indra-ocr/blob/v1.1.0/LICENSE)
before redistributing the image.

The Documents agent does not call an LLM. Converted Markdown is written
directly from Indra or MarkItDown output, with conversion and OCR metadata.

`docker-compose.yml` mounts `${WORKSPACES_ROOT}` at `/workspaces`. When
`documents_convert_to_markdown` receives `workspace`, converted Markdown is ready
for `wiki ingest` in that workspace without a copy step.

## MCP Authentication

When running via `wiki-workspace agents up`, set `DOCUMENTS_MCP_AUTH_TOKEN` in
the manager's `.env`; it is mapped to `MCP_AUTH_TOKEN` inside the container.

For standalone start:

```bash
export MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
docker compose up --build
```

MCP clients must send:

```txt
Authorization: Bearer <same-token>
```

If `MCP_AUTH_TOKEN` is empty, the server accepts unauthenticated requests.
Use that only for local debugging.

For scoped HTTP access, set `MCP_READ_TOKEN` for status-only clients and
`MCP_WRITE_TOKEN` for clients allowed to start conversions. `MCP_AUTH_TOKEN`
remains a legacy full-access read+write token. Rate limiting defaults to 120
requests per 60 seconds and can be tuned with `MCP_RATE_LIMIT_REQUESTS` and
`MCP_RATE_LIMIT_WINDOW_SECONDS`.

The conversion tool accepts either:

- `workspace`: optional target workspace. When set, output goes to that
  workspace's `raw/untracked/`.
- `filePath`: absolute path or path relative to the selected workspace or
  `DOCUMENT_INPUT_DIR`.
- `base64Content` plus `filename`: direct upload-style conversion.

Optional arguments:

- `outputFilename`: Markdown filename written under the workspace output
  directory or `DOCUMENT_OUTPUT_DIR` when no workspace is provided.
- `includeMetadata`: include YAML front matter, defaults to true.

For image files and PDFs, OCR runs through the local Indra model. The document
is not sent to a vision provider for OCR. Indra extracts text and layout; it does
not turn diagrams into Mermaid. The agent writes converted Markdown directly
from Indra or MarkItDown output and does not read the workspace's `.wikirc.yaml`
or call an LLM. Front matter records the conversion method and OCR status where
applicable.

The local correction screen is available at:

```txt
http://localhost:3337/correction
```

Indra's code is Apache 2.0; its model weights use a modified OpenRAIL-M license.
Review the model terms for your intended commercial use and redistribution.

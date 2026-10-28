# agent-documents

Current coordinated release: **0.16.65** (trust `_AGENT_VERSION` and the git tag, not this line). Keep `_AGENT_VERSION` aligned with
the coordinated workspace stack.

`agent-documents` is an external MCP Streamable HTTP server for document
ingestion. It converts PDFs, Office files, text files and images into Markdown
for wiki ingestion workflows.

## Files

- `document_mcp_server.py`: Starlette/uvicorn MCP server with bearer-auth
  middleware and conversion tools.
- `Dockerfile`: runtime image with MarkItDown and local Indra OCR.
- `docker-compose.yml`: standalone local service on port `3337` by default.
- `documents/input`: mounted input directory for local files.
- `WORKSPACES_ROOT`: container root for all workspaces. When a conversion call
  includes `workspace`, output is written to
  `/workspaces/<workspace>/raw/untracked/`.
- `DOCUMENT_OUTPUT_DIR`: fallback output directory for inline/no-workspace
  conversions.

## Tools

- `documents_status`: checks configuration and converter availability.
- `documents_convert_to_markdown`: converts one file to Markdown from either a
  mounted `filePath` or `base64Content` plus `filename`.

OCR degradation: Indra model initialization or page-conversion
failures must not fail the conversion. Images return a clearly marked fallback
stub; PDFs preserve extractable text when possible and mark OCR as skipped with
the reason (`_ocr_skipped_reason`). Keep this behavior when touching the
conversion paths. OCR itself must remain local and must not call an LLM or the
MarkItDown OCR plugin.

Conversion must remain LLM-free. `_run_conversion_job` writes the output from
`_convert_file` directly, with source, method and OCR metadata. It must not read
the workspace `.wikirc.yaml`, send converted content to a provider, or add a
post-conversion polishing pass. This keeps conversion time independent of LLM
availability and preserves the exact MarkItDown/Indra output.

## Safety

Do not expose this service publicly without `MCP_AUTH_TOKEN`. Uploaded and
converted documents may contain sensitive information; keep input and output
volumes local or encrypted in production.

Set `WORKSPACES_ROOT` before `docker compose up`; generated Markdown is written
to the workspace selected by the `workspace` tool argument.

**Auth, scopes, rate limiting** (0.10.3): `MCP_AUTH_TOKEN` remains a legacy
full-access (read+write) token; `MCP_READ_TOKEN`/`MCP_WRITE_TOKEN` grant
scoped access instead. `_token_scopes` compares with `hmac.compare_digest`
(constant-time). `_require_tool_scope` denies `_WRITE_TOOLS`
(`documents_convert_to_markdown`) to read-only callers; the current
request's scope is threaded through a `contextvars.ContextVar` set by
`_BearerAuthMiddleware`, not passed explicitly. Requests are rate-limited
(`MCP_RATE_LIMIT_REQUESTS`/`MCP_RATE_LIMIT_WINDOW_SECONDS`, default 120/60s)
keyed by token or remote IP. `_any_token_configured()` is the single "is any
token set" check. This whole block is copy-pasted near-verbatim across all
four agent repos plus `llm-wiki`'s `mcpHttp.ts` (TypeScript) — see
`agent-cme/CLAUDE.md`'s fuller note on why that hasn't been consolidated
into a shared package.

**Multi-user status**: the wikiLLM workspace remains a single-user deployment
baseline; the multi-user model is specified in
`llm-wiki/docs/industrialisation.md` and planned next — see
`agent-cme/CLAUDE.md`'s fuller note. This agent's token scoping is read/write,
not per-user; do not deploy it as a shared endpoint for distinct end users
before that lot lands.

Keep `_AGENT_VERSION` aligned with the coordinated `llm-wiki-manager` release
version so status responses identify the deployed agent bundle. Current release
line: `0.16.65`. Alignment is checked by `llm-wiki-manager/scripts/check-versions.js`
and synced by the root `build-and-push.sh`.

MCP tool descriptions, `_activity` metadata, conversion progress labels,
status/correction pages, and operator-facing errors must stay in English. Indra
OCR preserves the recognized document language.

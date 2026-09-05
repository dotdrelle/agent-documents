# agent-documents

Current coordinated release: **0.15.66**. Keep `_AGENT_VERSION` aligned with
the coordinated workspace stack.

`agent-documents` is an external MCP Streamable HTTP server for document
ingestion. It converts PDFs, Office files, text files and images into Markdown
for wiki ingestion workflows.

## Files

- `document_mcp_server.py`: Starlette/uvicorn MCP server with bearer-auth
  middleware and conversion tools.
- `Dockerfile`: runtime image with MarkItDown, Poppler and LLM OCR support.
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

OCR degradation (0.12.0 corrective release): LLM-OCR/embedding failures must
not fail the conversion. On such errors the server falls back to plain
conversion (MarkItDown/PDF extraction without OCR) and flags the result with
the skip reason (`_ocr_skipped_reason`). Keep this behavior when touching the
conversion paths.

Markdown polish: every conversion whose Markdown was produced by a machine
transformation (full or partial LLM-OCR of a PDF, MarkItDown output of a PDF
or an Office file, OCR of an image) gets two finishing steps
(`_polish_converted_markdown`, called from `_run_conversion_job` after
`_convert_file` and **before the file is written**). First a deterministic,
LLM-free cleanup (`_cleanup_markdown`): leftover HTML is converted to
Markdown by a small stdlib `HTMLParser` (`_SimpleHtmlParser` — boxes become
plain lines, HTML tables become Markdown tables, `style`/`script` dropped,
fenced code blocks are never touched), references to image files that are not
part of the document are removed, links split across lines are rejoined,
blank lines and trailing spaces are normalized, and indented headings are
un-indented. This pass costs nothing and scales to any size, so a conversion
is never written as raw HTML soup even when no LLM is reachable. Then the LLM
polish pass (`_polish_markdown`) does the semantic harmonization: repair
broken Markdown syntax, rejoin corrupted links, drop OCR/encoding errors and
stray artifacts, harmonize heading levels, and reflow tables or lists split
across a page break — without altering any fact, label or table cell. Plain
text files (`text`) and the degraded stubs (`image-fallback`,
`pdf-fallback`) are exempt from both: human-authored text must not be
rewritten, and a stub has no content to clean. The prompt takes the
workspace language from the top-level `language` key of
`<workspace>/.wikirc.yaml` (`_workspace_language` reads that single key;
nothing else from the file is touched or logged) and instructs the model to
keep that language. Same degradation rule as OCR applies: a failure never
fails the conversion, it falls back to the unpolished (but deterministically
cleaned) markdown and the skip reason lands in the `polish` frontmatter key
(`skipped (…)`), alongside the `ocr` key — so both statuses surface
independently in the frontmatter and in `documents_conversion_status`. A
successful polish records `polish: "done"`; the key is absent only when no
polish was attempted (exempt method, or no LLM configured).

The polish call reuses the workspace's own LLM: `_polish_llm_config` reads the
`llm` block (`baseUrl`, `model`, `apiKey`) of `<workspace>/.wikirc.yaml`
(`_wikirc_llm_block`; only that block is read, nothing else from the file is
touched or logged, and the key only ever goes into the Authorization header).
That is the model the workspace already uses for everything else — no second
model to configure. When there is no workspace or no usable `llm` block, it
falls back to the agent's own `DOCUMENT_LLM_*` configuration. This replaced an
earlier `DOCUMENT_POLISH_LLM_MODEL` env var, which duplicated a whole model
configuration that already lived in `.wikirc.yaml`.

Degenerate-output guard: both the per-page OCR call and the polish call run at
`temperature=0` (greedy decoding) with no repetition penalty, which can lock a
vision model into repeating the same short line (e.g. a page-number footer
like "12/18") hundreds of times instead of stopping — observed in practice on
a page the model otherwise failed to read. `_looks_degenerate` counts
non-empty lines (heading markers stripped, so "# 12/18" and "## 12/18" count
as the same line) and flags the output only when a line repeats more than
`_LLM_MAX_LINE_REPEATS` (10) times **and** those repeats make up more than
half the non-empty lines — the fraction matters: a real document carries a
footer table on every page ("Titre / Référence / Date", 17 times in an
18-page PDF) and the first version of the guard treated that as runaway
repetition and discarded a perfectly polished document. A failure raises
instead of returning the runaway content — it then flows through the same
skip-reason fallback as any other OCR/polish failure. `_LLM_OCR_MAX_TOKENS`
(8192) bounds one page's OCR call.
The one exception to `temperature=0`: gpt-5-class models refuse the
`temperature` parameter outright (same documented refusal as llm-wiki's
`engineCapabilities`), so `_model_refuses_temperature` omits it from both
payloads when the bare model name matches `gpt-5` — without this, every call
against the default `gpt-5.4-mini` would fail with a 400 and silently degrade
to the unpolished output.

The polish call sends no `max_tokens` at all — an earlier version tried to
compute one (first a flat guess from the input's character count, then a
retry loop that parsed the model's real context size out of its own 400
error). Both attempts fought the same problem from the wrong end: guessing an
output budget for a whole-document reformat, on a model whose context window
we don't control and whose real per-request token count can drift a few
hundred tokens between otherwise-identical calls. The safety nets don't need
that number: `_looks_degenerate` (see above) catches a runaway repetition
regardless of length, and `finish_reason == "length"` on the response tells
us, after the fact and for free, whether the model ran out of room.

A document whose whole-document call fails for size (`_PolishSizeError`:
`finish_reason == "length"`, or an HTTP 400 such as a context-length refusal)
is then polished **in chunks**: `_split_markdown_chunks` cuts the Markdown at
heading boundaries (falling back to paragraph boundaries — a converted
document often has no headings at all) into pieces of at most
`_POLISH_CHUNK_MAX_CHARS` (10000) characters, and each piece gets its own
polish call, dispatched over a small thread pool (`_POLISH_CHUNK_WORKERS` =
3) so a big document costs parallel batches, not one call per chunk in
series. There is deliberately **no document-size cap**: a 120 KB file is
polished like a 12 KB one, only with more chunks — that is the answer to
"files three times this size will never fit", they do not need to fit, only
the chunks do. This was added after a real 40 KB specification PDF came back
`polish: skipped (output was truncated)`: one call had to rewrite the whole
document, hit the model's default output cap, and the fallback silently
returned the raw conversion the polish existed to clean. Transient failures
(network, timeout, bad JSON) still skip without retry — chunking would only
multiply the noise. Chunk statuses are announced, never silent: all chunks
ok → `done`; some chunks failed → `partial (N/M chunk(s) skipped: <reason>)`
with the failed chunks kept verbatim; all chunks failed → `skipped (...)` with
the original returned. The per-chunk degenerate guard and empty-output check
apply to every chunk.

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
line: `0.12.0`. Alignment is checked by `llm-wiki-manager/scripts/check-versions.js`
and synced by the root `build-and-push.sh`.

MCP tool descriptions, `_activity` metadata, conversion progress labels,
status/correction pages, and operator-facing errors must stay in English. OCR
and image-to-Markdown prompts may instruct the LLM to preserve the original
document language, but the service UI itself is not localized from `.wikirc`.

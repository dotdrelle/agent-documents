#!/usr/bin/env python3
"""Document ingestion MCP Server - convert files to Markdown with local OCR."""

import base64
import contextvars
import contextlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send
import uvicorn


app = Server("agent-documents")

_AGENT_VERSION = "0.16.68"
_MCP_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")
_DOCUMENT_INPUT_DIR = Path(os.environ.get("DOCUMENT_INPUT_DIR", "/documents/input")).resolve()
_DOCUMENT_OUTPUT_DIR = Path(os.environ.get("DOCUMENT_OUTPUT_DIR", "/documents/output")).resolve()
_WORKSPACES_ROOT = Path(os.environ.get("WORKSPACES_ROOT", "/workspaces")).resolve()
_MAX_UPLOAD_BYTES = int(os.environ.get("DOCUMENT_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
_INDRA_CACHE_DIR = Path(
    os.environ.get("HF_HOME", str(Path.home() / ".cache" / "huggingface"))
).resolve()
_INDRA_THREADS = max(1, int(os.environ.get("INDRA_OCR_THREADS", "4")))
_OFFICE_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_OFFICE_IMAGE_MAX_FILES = 100
_OFFICE_IMAGE_MAX_BYTES = 20 * 1024 * 1024
_OFFICE_IMAGE_MAX_TOTAL_BYTES = 100 * 1024 * 1024
_INDRA_CONVERTER: Any = None
_INDRA_LOCK = threading.Lock()

_conversion_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()

_CONVERSION_PLAN_STEPS = [
    {"id": "resolve", "label": "Resolve source file"},
    {"id": "convert", "label": "Convert to Markdown"},
    {"id": "write", "label": "Write converted Markdown"},
]

_MCP_READ_TOKEN = os.environ.get("MCP_READ_TOKEN", "")
_MCP_WRITE_TOKEN = os.environ.get("MCP_WRITE_TOKEN", "")
_CURRENT_SCOPES: contextvars.ContextVar[set[str]] = contextvars.ContextVar("mcp_scopes", default={"read", "write"})
_WRITE_TOOLS = {"documents_convert_to_markdown"}
_RATE_LIMIT_REQUESTS = int(os.environ.get("MCP_RATE_LIMIT_REQUESTS", "120"))
_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("MCP_RATE_LIMIT_WINDOW_SECONDS", "60"))
_RATE_BUCKETS: dict[str, list[float]] = {}

def _any_token_configured() -> bool:
    return bool(_MCP_TOKEN or _MCP_READ_TOKEN or _MCP_WRITE_TOKEN)


if not _any_token_configured():
    print("[document-mcp] Warning: MCP_AUTH_TOKEN is not configured; the endpoint accepts unauthenticated clients.")


def _bearer_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth[7:] if auth.lower().startswith("bearer ") else ""


def _token_scopes(token: str) -> set[str] | None:
    if not _any_token_configured():
        return {"read", "write"}
    if _MCP_TOKEN and hmac.compare_digest(token, _MCP_TOKEN):
        return {"read", "write"}
    if _MCP_WRITE_TOKEN and hmac.compare_digest(token, _MCP_WRITE_TOKEN):
        return {"read", "write"}
    if _MCP_READ_TOKEN and hmac.compare_digest(token, _MCP_READ_TOKEN):
        return {"read"}
    return None


def _require_tool_scope(name: str) -> list[TextContent] | None:
    if name in _WRITE_TOOLS and "write" not in _CURRENT_SCOPES.get():
        return _json_text({"ok": False, "error": f"token does not have write scope for {name}"})
    return None


def _rate_limit_key(request: Request, token: str) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    host = forwarded or (request.client.host if request.client else "unknown")
    return f"token:{token}" if token else f"ip:{host}"


def _rate_limited(key: str) -> bool:
    now = time.time()
    cutoff = now - max(1, _RATE_LIMIT_WINDOW_SECONDS)
    bucket = [item for item in _RATE_BUCKETS.get(key, []) if item > cutoff]
    if len(bucket) >= max(1, _RATE_LIMIT_REQUESTS):
        _RATE_BUCKETS[key] = bucket
        return True
    bucket.append(now)
    _RATE_BUCKETS[key] = bucket
    return False


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method == "GET" and _wants_html(request):
            return await call_next(request)
        token_value = _bearer_token(request)
        scopes = _token_scopes(token_value)
        if scopes is None:
            return PlainTextResponse("Unauthorized", status_code=401)
        if _rate_limited(_rate_limit_key(request, token_value)):
            return PlainTextResponse("Rate limit exceeded", status_code=429)
        token = _CURRENT_SCOPES.set(scopes)
        try:
            return await call_next(request)
        finally:
            _CURRENT_SCOPES.reset(token)


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _json_text(payload: dict[str, Any]) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, indent=2))]


def _mask_secret_text(value: Any) -> str:
    text = str(value)
    text = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s,;]+", r"\1***", text)
    text = re.sub(r"(?i)(api[_-]?key|token|secret|password)(['\"]?\s*[:=]\s*['\"]?)[^'\"\s,;]+", r"\1\2***", text)
    return text


_STEP_INDEX = {"resolve": 1, "convert": 2, "write": 3}
_STEP_PERCENT = {"resolve": 10, "convert": 60, "write": 90}


def _activity_for_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    status = job["status"]
    terminal = status in {"done", "failed", "error"}
    step_id = job.get("stepId", "resolve")
    step_index = _STEP_INDEX.get(step_id, 1)
    percent = 100 if status == "done" else (_STEP_PERCENT.get(step_id, 0) if status == "running" else 0)
    source_name = job.get("sourceName") or job_id
    return {
        "id": f"documents:{source_name}",
        "source": "documents",
        "kind": "conversion",
        "label": f"Documents: conversion {source_name}",
        "status": status,
        "progress": {
            "percent": percent,
            "step": "conversion",
            "stepId": step_id,
            "stepIndex": step_index,
            "stepTotal": 3,
            "method": job.get("method"),
            "detail": job.get("detail"),
        },
        "plan": {"steps": _CONVERSION_PLAN_STEPS},
        "poll": {
            "server": "documents",
            "tool": "documents_conversion_status",
            "args": {"jobId": job_id},
            "intervalMs": 2500,
        },
        "startedAt": job.get("startedAt"),
        "updatedAt": _utc_now(),
        "error": job.get("error"),
        "terminal": terminal,
    }


def _parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError(f"Expected a boolean, got: {value!r}")


def _run_conversion_job(job_id: str, args: dict[str, Any]) -> None:
    def update(step_id: str, detail: str | None = None) -> None:
        with _jobs_lock:
            _conversion_jobs[job_id]["stepId"] = step_id
            _conversion_jobs[job_id]["detail"] = detail

    tmpdir_obj = tempfile.TemporaryDirectory(prefix="agent-documents-")
    tmpdir = Path(tmpdir_obj.name)
    try:
        update("resolve", "Resolving source file")
        workspace = str(args.get("workspace", "") or "").strip()
        workspace_path = _validate_workspace(workspace) if workspace else None
        output_dir = (workspace_path / "raw" / "untracked") if workspace_path else _DOCUMENT_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        include_metadata = _parse_bool(args.get("includeMetadata"), True)
        source = _resolve_source(args, tmpdir, workspace_path)
        with _jobs_lock:
            _conversion_jobs[job_id]["sourceName"] = source.name
            _conversion_jobs[job_id]["sourceStr"] = str(source)

        update("convert", f"Converting {source.name}")
        markdown, method, ocr_status = _convert_file(source, tmpdir)
        with _jobs_lock:
            _conversion_jobs[job_id]["method"] = method
            _conversion_jobs[job_id]["ocr"] = ocr_status

        update("write", "Writing converted Markdown")
        output_name = _safe_output_name(args.get("outputFilename"), source)
        output_path = output_dir / output_name
        final_markdown = _with_metadata(markdown, source, method, ocr_status) if include_metadata else markdown
        output_path.write_text(final_markdown, encoding="utf-8")
        with _jobs_lock:
            _conversion_jobs[job_id].update({
                "status": "done",
                "stepId": "write",
                "outputPath": str(output_path),
                "bytes": output_path.stat().st_size,
                "markdown": final_markdown,
                "detail": None,
            })
    except Exception as exc:
        with _jobs_lock:
            _conversion_jobs[job_id]["status"] = "failed"
            _conversion_jobs[job_id]["error"] = str(exc)
            _conversion_jobs[job_id]["detail"] = None
        print(f"[document-mcp] conversion job {job_id} failed: {_mask_secret_text(exc)}")
    finally:
        tmpdir_obj.cleanup()


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _validate_workspace(name: str) -> Path:
    value = str(name or "").strip()
    if not value or "/" in value or "\\" in value or value in {".", ".."} or ".." in value:
        raise ValueError(f"Invalid workspace: {name}")
    path = (_WORKSPACES_ROOT / value).resolve()
    try:
        path.relative_to(_WORKSPACES_ROOT)
    except ValueError as exc:
        raise ValueError("Path traversal attempt") from exc
    if not path.is_dir():
        raise ValueError(f"Unknown workspace: {value}")
    return path


def _ensure_inside(path: Path, roots: list[Path]) -> None:
    for root in roots:
        try:
            path.relative_to(root)
            return
        except ValueError:
            continue
    allowed = ", ".join(str(root) for root in roots)
    raise ValueError(f"Input path is outside allowed roots: {allowed}")


def _render_landing_page(endpoint_url: str, scheme: str) -> str:
    auth_status = (
        "Bearer token enabled"
        if _MCP_TOKEN
        else "Warning: MCP_AUTH_TOKEN is not configured; the endpoint accepts unauthenticated clients."
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>agent-documents MCP connector</title>
  <style>
    :root {{ color-scheme: light dark; --bg:#f8fafc; --panel:#fff; --text:#111827; --muted:#64748b; --line:#d8dee8; --accent:#2563eb; --code:#eef2ff; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f172a; --panel:#111827; --text:#f8fafc; --muted:#94a3b8; --line:#253044; --accent:#60a5fa; --code:#1e293b; }} }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; min-height:100vh; background:var(--bg); color:var(--text); font:15px/1.55 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
    main {{ width:min(920px,calc(100% - 32px)); margin:0 auto; padding:56px 0; }}
    .eyebrow {{ color:var(--accent); font-weight:700; letter-spacing:.04em; text-transform:uppercase; font-size:12px; }}
    h1 {{ margin:8px 0 10px; font-size:clamp(32px,6vw,52px); line-height:1.05; letter-spacing:0; }}
    h2 {{ margin:0 0 16px; font-size:20px; letter-spacing:0; }}
    .lead {{ margin:0 0 28px; color:var(--muted); max-width:720px; font-size:17px; }}
    .panel {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:22px; margin:18px 0; }}
    dl {{ display:grid; grid-template-columns:170px 1fr; gap:10px 18px; margin:0; }}
    dt {{ color:var(--muted); }}
    dd {{ margin:0; min-width:0; overflow-wrap:anywhere; }}
    code {{ background:var(--code); border:1px solid var(--line); border-radius:6px; padding:2px 6px; font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; font-size:13px; }}
    ul {{ list-style:none; margin:0; padding:0; display:grid; gap:10px; }}
    li {{ display:grid; grid-template-columns:minmax(170px,240px) 1fr; gap:14px; align-items:start; padding:12px 0; border-top:1px solid var(--line); }}
    li:first-child {{ border-top:0; padding-top:0; }}
    li span {{ color:var(--muted); }}
    @media (max-width:640px) {{ dl,li {{ grid-template-columns:1fr; }} main {{ padding:32px 0; }} }}
  </style>
</head>
<body>
  <main>
    <div class="eyebrow">MCP Streamable HTTP</div>
    <h1>agent-documents MCP connector</h1>
    <p class="lead">Document ingestion agent for converting PDF, Office, text and image files into Markdown.</p>
    <section class="panel">
      <dl>
        <dt>Status</dt><dd>Ready</dd>
        <dt>Version</dt><dd><code>{_escape_html(_AGENT_VERSION)}</code></dd>
        <dt>Endpoint</dt><dd><code>{_escape_html(endpoint_url)}</code></dd>
        <dt>Transport</dt><dd>MCP Streamable HTTP over {_escape_html(scheme.upper())}</dd>
        <dt>Authentication</dt><dd>{_escape_html(auth_status)}</dd>
        <dt>Input dir</dt><dd><code>{_escape_html(str(_DOCUMENT_INPUT_DIR))}</code></dd>
        <dt>Output dir</dt><dd><code>{_escape_html(str(_DOCUMENT_OUTPUT_DIR))}</code></dd>
        <dt>Workspaces root</dt><dd><code>{_escape_html(str(_WORKSPACES_ROOT))}</code></dd>
        <dt>OCR</dt><dd>Local Indra OCR / CPU / cache <code>{_escape_html(str(_INDRA_CACHE_DIR))}</code></dd>
      </dl>
    </section>
    <section class="panel">
      <h2>Available tools</h2>
      <ul>
        <li><code>documents_status</code><span>Check conversion runtime and available OCR tools.</span></li>
        <li><code>documents_convert_to_markdown</code><span>Convert one uploaded or mounted document to Markdown.</span></li>
      </ul>
    </section>
  </main>
</body>
</html>"""


def _render_correction_page() -> str:
    return """<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Correction Mermaid locale</title>
  <style>
    :root { color-scheme: light dark; --bg:#f8fafc; --panel:#fff; --text:#111827; --muted:#64748b; --line:#d8dee8; --accent:#2563eb; --code:#eef2ff; }
    @media (prefers-color-scheme: dark) { :root { --bg:#0f172a; --panel:#111827; --text:#f8fafc; --muted:#94a3b8; --line:#253044; --accent:#60a5fa; --code:#1e293b; } }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:var(--bg); color:var(--text); font:14px/1.45 ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    main { width:min(1180px,calc(100% - 32px)); margin:0 auto; padding:32px 0; }
    h1 { margin:0 0 16px; font-size:28px; letter-spacing:0; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:16px; align-items:start; }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; min-width:0; }
    label { display:block; color:var(--muted); font-weight:700; margin:0 0 8px; }
    textarea { width:100%; min-height:320px; resize:vertical; border:1px solid var(--line); border-radius:6px; background:transparent; color:var(--text); padding:10px; font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; }
    button { border:1px solid var(--line); border-radius:6px; background:var(--accent); color:white; padding:8px 12px; font-weight:700; cursor:pointer; }
    .toolbar { display:flex; gap:8px; flex-wrap:wrap; margin:12px 0; }
    pre { min-height:180px; white-space:pre-wrap; overflow:auto; border:1px solid var(--line); border-radius:6px; background:var(--code); padding:10px; margin:0; font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace; }
    @media (max-width:820px) { .grid { grid-template-columns:1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>Correction Mermaid locale</h1>
    <div class="grid">
      <section class="panel">
        <label for="ocr">OCR / notes de correction</label>
        <textarea id="ocr" spellcheck="false"></textarea>
      </section>
      <section class="panel">
        <label for="mermaid">Corrected Mermaid</label>
        <textarea id="mermaid" spellcheck="false">flowchart LR
  A["Source"] --> B["Cible"]</textarea>
      </section>
    </div>
    <div class="toolbar">
      <button type="button" onclick="buildMarkdown()">Generate Markdown</button>
      <button type="button" onclick="copyMarkdown()">Copy Markdown</button>
    </div>
    <section class="panel">
      <label for="markdown">Corrected Markdown</label>
      <pre id="markdown"></pre>
    </section>
  </main>
  <script>
    function buildMarkdown() {
      const ocr = document.getElementById('ocr').value.trim();
      const mermaid = document.getElementById('mermaid').value.trim();
      const parts = [];
      if (ocr) parts.push(ocr);
      if (mermaid) parts.push('## Mermaid Diagram\\n\\n```mermaid\\n' + mermaid + '\\n```');
      document.getElementById('markdown').textContent = parts.join('\\n\\n');
    }
    async function copyMarkdown() {
      buildMarkdown();
      await navigator.clipboard.writeText(document.getElementById('markdown').textContent);
    }
    buildMarkdown();
  </script>
</body>
</html>"""


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="documents_status",
            description="Check agent-documents configuration, directories and available conversion tools.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        Tool(
            name="documents_convert_to_markdown",
            description=(
                "Convert a PDF, Office document, text file or image to Markdown. This is the pre-ingestion step: it turns a "
                "non-Markdown document into Markdown so it can afterwards be ingested into llm-wiki. When a workspace is given, "
                "the Markdown is written under <workspace>/raw/untracked, where the ingestion step looks for pending sources. "
                "Starts an async job and returns immediately with a jobId and a running _activity. Poll documents_conversion_status to track progress. "
                "Provide either filePath for a file mounted under DOCUMENT_INPUT_DIR, or base64Content plus filename "
                "for direct upload."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filePath": {
                        "type": "string",
                        "description": "Path to an input file, absolute or relative to the workspace or DOCUMENT_INPUT_DIR.",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Optional target workspace name. When set, Markdown is written to <workspace>/raw/untracked.",
                    },
                    "filename": {
                        "type": "string",
                        "description": "Original filename when base64Content is provided.",
                    },
                    "base64Content": {
                        "type": "string",
                        "description": "Base64 encoded file content for upload-style conversion.",
                    },
                    "outputFilename": {
                        "type": "string",
                        "description": "Optional Markdown output filename. Defaults to the input stem plus .md.",
                    },
                    "includeMetadata": {
                        "type": "boolean",
                        "description": "Include Markdown front matter metadata. Defaults to true.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        Tool(
            name="documents_conversion_status",
            description="Poll the status of an async documents_convert_to_markdown job. Returns _activity with progress and, when done, the conversion result.",
            inputSchema={
                "type": "object",
                "properties": {
                    "jobId": {"type": "string", "description": "Job ID returned by documents_convert_to_markdown."},
                },
                "required": ["jobId"],
                "additionalProperties": False,
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    start = time.time()
    print(f"[document-mcp] tools/call {name}")
    try:
        denied = _require_tool_scope(name)
        if denied is not None:
            return denied
        match name:
            case "documents_status":
                result = _tool_status()
            case "documents_convert_to_markdown":
                result = _tool_convert_to_markdown(arguments)
            case "documents_conversion_status":
                result = _tool_conversion_status(arguments)
            case _:
                raise ValueError(f"Unknown tool: {name}")
        print(f"[document-mcp] tools/result {name} ok {int((time.time() - start) * 1000)}ms")
        return result
    except Exception as exc:
        print(f"[document-mcp] tools/result {name} error {int((time.time() - start) * 1000)}ms {_mask_secret_text(exc)}")
        return _json_text({"ok": False, "error": str(exc)})


def _tool_status() -> list[TextContent]:
    return _json_text(
        {
            "ok": True,
            "service": "agent-documents",
            "version": _AGENT_VERSION,
            "inputDir": str(_DOCUMENT_INPUT_DIR),
            "outputDir": str(_DOCUMENT_OUTPUT_DIR),
            "workspacesRoot": str(_WORKSPACES_ROOT),
            "maxUploadBytes": _MAX_UPLOAD_BYTES,
            "ocr": {
                "pipeline": "indra-local-markdown",
                "engine": "indra-ocr",
                "modelDir": os.environ.get("INDRA_OCR_MODEL_DIR", str(_INDRA_CACHE_DIR)),
                "threads": _INDRA_THREADS,
                "correctionScreen": "/correction",
            },
            "office": {"converter": "markitdown"},
            **({"mmdc": {"available": True}} if shutil.which("mmdc") else {}),
            "supportedExtensions": sorted(_SUPPORTED_EXTENSIONS),
        }
    )


def _tool_convert_to_markdown(args: dict[str, Any]) -> list[TextContent]:
    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _conversion_jobs[job_id] = {
            "status": "running",
            "stepId": "resolve",
            "startedAt": _utc_now(),
            "sourceName": None,
            "sourceStr": None,
            "method": None,
            "ocr": None,
            "outputPath": None,
            "bytes": None,
            "markdown": None,
            "error": None,
            "detail": None,
        }
    threading.Thread(target=_run_conversion_job, args=(job_id, args), daemon=True).start()
    with _jobs_lock:
        job = dict(_conversion_jobs[job_id])
    return _json_text({"ok": True, "jobId": job_id, "_activity": _activity_for_job(job_id, job)})


def _tool_conversion_status(args: dict[str, Any]) -> list[TextContent]:
    job_id = str(args.get("jobId", "") or "").strip()
    with _jobs_lock:
        job = dict(_conversion_jobs.get(job_id) or {})
    if not job:
        return _json_text({"ok": False, "error": f"Unknown job: {job_id}"})
    activity = _activity_for_job(job_id, job)
    result: dict[str, Any] = {"ok": True, "jobId": job_id, "_activity": activity}
    if job.get("status") == "done":
        result.update({
            "outputPath": job.get("outputPath"),
            "method": job.get("method"),
            "ocr": job.get("ocr"),
            "bytes": job.get("bytes"),
            "markdown": job.get("markdown"),
        })
    elif job.get("status") in {"failed", "error"}:
        result["error"] = job.get("error")
    return _json_text(result)


def _resolve_source(args: dict[str, Any], tmpdir: Path, workspace_path: Path | None) -> Path:
    file_path = str(args.get("filePath", "") or "").strip()
    base64_content = str(args.get("base64Content", "") or "").strip()
    if file_path and base64_content:
        raise ValueError("Provide either filePath or base64Content, not both.")
    if file_path:
        raw_path = Path(file_path)
        if raw_path.is_absolute():
            path = raw_path.resolve()
        elif workspace_path:
            workspace_candidate = (workspace_path / raw_path).resolve()
            input_candidate = (_DOCUMENT_INPUT_DIR / raw_path).resolve()
            path = workspace_candidate if workspace_candidate.is_file() else input_candidate
        else:
            path = (_DOCUMENT_INPUT_DIR / raw_path).resolve()
        allowed_roots = [_DOCUMENT_INPUT_DIR]
        if workspace_path:
            # The workspace contributes only its ingestion staging area, never
            # the workspace root: `.wikirc.yaml` (llm.apiKey / mcp.accessKey)
            # and other secrets live at the root and are `.yaml`/`.md` text
            # files this server would otherwise hand back via conversion status.
            allowed_roots.insert(0, (workspace_path / "raw" / "untracked").resolve())
        _ensure_inside(path, allowed_roots)
        if not path.is_file():
            raise ValueError(f"Input file does not exist: {path}")
        return path
    if base64_content:
        filename = _safe_filename(str(args.get("filename", "") or "").strip() or "upload.bin")
        try:
            data = base64.b64decode(base64_content, validate=True)
        except Exception as exc:
            raise ValueError("base64Content is not valid base64.") from exc
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError(f"Upload is too large. Limit is {_MAX_UPLOAD_BYTES} bytes.")
        path = tmpdir / filename
        path.write_bytes(data)
        return path
    raise ValueError("Provide filePath or base64Content.")


_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".csv", ".json", ".xml", ".yaml", ".yml", ".html", ".htm", ".rtf"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
_OFFICE_EXTENSIONS = {".docx", ".pptx", ".xlsx", ".doc", ".ppt", ".xls", ".odt", ".ods", ".odp"}
_SUPPORTED_EXTENSIONS = _TEXT_EXTENSIONS | _IMAGE_EXTENSIONS | _OFFICE_EXTENSIONS | {".pdf"}


def _convert_file(path: Path, tmpdir: Path) -> tuple[str, str, str | None]:
    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        mime, _ = mimetypes.guess_type(path.name)
        raise ValueError(f"Unsupported file type: {suffix or mime or 'unknown'}")
    if suffix in _TEXT_EXTENSIONS:
        return _convert_text(path), "text", None
    if suffix in _OFFICE_EXTENSIONS:
        markdown, ocr_status = _convert_office(path, tmpdir)
        return markdown, f"markitdown{suffix}", ocr_status
    if suffix in _IMAGE_EXTENSIONS:
        try:
            markdown = _indra_ocr(path)
        except Exception as exc:
            reason = _ocr_skipped_reason(exc)
            return _fallback_visual_markdown(path, reason), "image-fallback", reason
        return markdown, "image-indra-ocr", "done"
    if suffix == ".pdf":
        return _convert_pdf(path, tmpdir)
    raise ValueError(f"Unsupported file type: {suffix}")


def _convert_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return text if path.suffix.lower() in {".md", ".markdown"} else _plain_text_to_markdown(text)


def _convert_markitdown(path: Path) -> str:
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise ValueError("MarkItDown is required for this document type.") from exc

    try:
        result = MarkItDown(enable_plugins=False).convert(str(path))
    except Exception as exc:
        raise ValueError(f"MarkItDown conversion failed: {exc}") from exc

    markdown = str(
        getattr(result, "text_content", None)
        or getattr(result, "markdown", None)
        or ""
    ).strip()
    markdown = _normalize_mermaid_blocks(markdown)
    if not markdown:
        raise ValueError("MarkItDown returned empty Markdown.")
    return markdown + "\n"


def _convert_office(path: Path, tmpdir: Path) -> tuple[str, str | None]:
    markdown = _convert_markitdown(path)
    images, skipped = _extract_office_images(path, tmpdir)
    recognized: list[str] = []
    for index, image in enumerate(images, start=1):
        try:
            image_markdown = _indra_ocr(image).strip()
        except Exception as exc:
            skipped.append(_mask_secret_text(exc))
            continue
        recognized.append(
            f"### Embedded image {index}\n\n{image_markdown}"
        )

    if recognized:
        markdown = (
            markdown.rstrip()
            + "\n\n## Embedded image OCR\n\n"
            + "\n\n".join(recognized)
            + "\n"
        )
    if not images and not skipped:
        return markdown, None
    if not skipped:
        return markdown, "done"
    if recognized:
        return markdown, f"partial ({len(skipped)} embedded image(s) skipped: {skipped[0]})"
    return markdown, _ocr_skipped_reason(ValueError(skipped[0]))


def _extract_office_images(path: Path, tmpdir: Path) -> tuple[list[Path], list[str]]:
    """Read bounded raster media from ZIP-based Office files without extracting archive paths."""
    if path.suffix.lower() not in {".docx", ".pptx", ".xlsx", ".odt", ".ods", ".odp"}:
        return [], []
    try:
        archive = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return [], [f"could not read embedded images: {type(exc).__name__}"]

    images: list[Path] = []
    skipped: list[str] = []
    total_bytes = 0
    media = [
        info for info in archive.infolist()
        if not info.is_dir()
        and Path(info.filename).suffix.lower() in _OFFICE_IMAGE_EXTENSIONS
        and (
            "/media/" in f"/{info.filename.lower()}"
            or "/pictures/" in f"/{info.filename.lower()}"
        )
    ]
    media.sort(key=lambda info: info.filename.casefold())
    try:
        for info in media:
            if len(images) >= _OFFICE_IMAGE_MAX_FILES:
                skipped.append(f"more than {_OFFICE_IMAGE_MAX_FILES} embedded images")
                break
            if info.file_size > _OFFICE_IMAGE_MAX_BYTES:
                skipped.append(f"embedded image exceeds {_OFFICE_IMAGE_MAX_BYTES} bytes")
                continue
            if total_bytes + info.file_size > _OFFICE_IMAGE_MAX_TOTAL_BYTES:
                skipped.append(f"embedded images exceed {_OFFICE_IMAGE_MAX_TOTAL_BYTES} bytes total")
                break
            data = archive.read(info)
            if len(data) != info.file_size:
                skipped.append("embedded image size did not match its archive entry")
                continue
            target = tmpdir / f"office-image-{len(images) + 1}{Path(info.filename).suffix.lower()}"
            target.write_bytes(data)
            images.append(target)
            total_bytes += len(data)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        skipped.append(f"could not read embedded image: {type(exc).__name__}")
    finally:
        archive.close()
    return images, skipped


def _extract_pdf_text(path: Path) -> str:
    try:
        import fitz
    except ImportError as exc:
        raise ValueError("PyMuPDF is required for PDF text extraction.") from exc
    chunks: list[str] = []
    with fitz.open(path) as document:
        for page in document:
            text = page.get_text("text").strip()
            if text:
                chunks.append(text)
    return "\n\n".join(chunks)


def _convert_pdf(path: Path, tmpdir: Path) -> tuple[str, str, str | None]:
    try:
        return _indra_ocr(path), "pdf-indra-ocr", "done"
    except Exception as exc:
        reason = _ocr_skipped_reason(exc)
        try:
            text = _extract_pdf_text(path)
        except Exception:
            text = ""
        return _fallback_pdf_markdown(path, text, reason), "pdf-fallback", reason


def _ocr_skipped_reason(exc: Exception) -> str:
    return f"skipped ({_mask_secret_text(exc)})"


def _fallback_visual_markdown(path: Path, reason: str) -> str:
    return f"# {path.stem}\n\nOCR: {reason}\n\nSource image: `{path.name}`\n"


def _fallback_pdf_markdown(path: Path, text: str, reason: str) -> str:
    body = text.strip() if text.strip() else f"OCR: {reason}\n\nSource PDF: `{path.name}`"
    return _plain_text_to_markdown(body)


def _indra_ocr(path: Path) -> str:
    """Convert an image or PDF locally with Indra OCR, preserving page order."""
    global _INDRA_CONVERTER
    with _INDRA_LOCK:
        if _INDRA_CONVERTER is None:
            try:
                os.environ.setdefault("HF_HOME", str(_INDRA_CACHE_DIR))
                import indra_ocr
            except ImportError as exc:
                raise ValueError("Indra OCR is required for PDF and image conversion.") from exc
            _INDRA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                _INDRA_CONVERTER = indra_ocr.load(
                    threads=_INDRA_THREADS,
                    mode="auto",
                    image_ocr=True,
                )
            except Exception as exc:
                raise ValueError(f"Could not initialize Indra OCR: {exc}") from exc

        chunks: list[str] = []
        try:
            for page_number, page in _INDRA_CONVERTER.convert(str(path)):
                if getattr(page, "source", None) == "error":
                    note = str(getattr(page, "note", "") or "page conversion failed")
                    raise ValueError(f"page {page_number}: {note}")
                markdown = str(
                    getattr(page, "markdown", None)
                    or getattr(page, "text", None)
                    or ""
                ).strip()
                if markdown:
                    chunks.append(markdown)
        except Exception as exc:
            raise ValueError(f"Indra OCR failed: {exc}") from exc

        result = "\n\n".join(chunks).strip()
        if not result:
            raise ValueError("Indra OCR returned empty Markdown.")
        return result + "\n"


def _normalize_mermaid_blocks(markdown: str) -> str:
    def normalize_block(match: re.Match[str]) -> str:
        body = match.group(1)
        lines = [_normalize_mermaid_line(line) for line in body.splitlines()]
        return "```mermaid\n" + "\n".join(lines).strip() + "\n```"

    return re.sub(r"```mermaid\s*\n(.*?)\n```", normalize_block, markdown, flags=re.DOTALL | re.IGNORECASE)


def _quote_mermaid_label(label: str) -> str:
    clean = label.strip().replace(r"\"", '"').replace(r"\n", "<br/>")
    clean = clean.replace('"', "'")
    return f'"{clean}"'


def _normalize_mermaid_line(line: str) -> str:
    line = line.replace(r"\"", '"').replace(r"\n", "<br/>")
    subgraph = re.match(r'^(\s*)subgraph\s+([A-Za-z][A-Za-z0-9_]*)\[([^\"].*?)\](\s*)$', line)
    if subgraph:
        return f"{subgraph.group(1)}subgraph {subgraph.group(2)}[{_quote_mermaid_label(subgraph.group(3))}]{subgraph.group(4)}"
    database = re.match(r'^(\s*)([A-Za-z][A-Za-z0-9_]*)\[\((.*?)\)\](\s*)$', line)
    if database and any(char in database.group(3) for char in ['(', ')', '<', '>', ':']):
        return f"{database.group(1)}{database.group(2)}[({_quote_mermaid_label(database.group(3))})]{database.group(4)}"
    node = re.match(r'^(\s*)([A-Za-z][A-Za-z0-9_]*)\[([^\[\"\(].*?)\](\s*)$', line)
    if node:
        return f"{node.group(1)}{node.group(2)}[{_quote_mermaid_label(node.group(3))}]{node.group(4)}"
    return line


def _plain_text_to_markdown(text: str) -> str:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line.strip())
        elif current:
            blocks.append(" ".join(current))
            current = []
    if current:
        blocks.append(" ".join(current))
    return "\n\n".join(blocks).strip() + "\n"


def _with_metadata(markdown: str, source: Path, method: str, ocr_status: str | None = None) -> str:
    title = source.stem.replace("_", " ").replace("-", " ").strip() or source.name
    front_matter = {
        "title": title,
        "source_file": source.name,
        "conversion_method": method,
        **({"ocr": ocr_status} if ocr_status else {}),
        "service": "agent-documents",
        "service_version": _AGENT_VERSION,
    }
    lines = ["---"]
    for key, value in front_matter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    lines.append("")
    return "\n".join(lines) + markdown


def _safe_output_name(value: Any, source: Path) -> str:
    filename = _safe_filename(str(value or "").strip() or f"{source.stem}.md")
    if not filename.lower().endswith(".md"):
        filename = f"{Path(filename).stem}.md"
    return filename


def _safe_filename(value: str) -> str:
    name = Path(value).name
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", ".", " "} else "_" for char in name).strip()
    return cleaned or "document"


def create_starlette_app() -> Starlette:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    streamable_http = StreamableHTTPSessionManager(
        app=app,
        event_store=None,
        json_response=False,
        stateless=True,
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if path not in {"/mcp", "/mcp/"}:
            response = PlainTextResponse("Not found", status_code=404)
            await response(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.method == "GET" and _wants_html(request):
            scheme = "https" if request.url.scheme == "https" else "http"
            endpoint_url = f"{scheme}://{request.headers.get('host', f'{host}:{port}')}/mcp/"
            response = HTMLResponse(_render_landing_page(endpoint_url, scheme))
            await response(scope, receive, send)
            return

        mcp_scope = dict(scope)
        mcp_scope["path"] = "/"
        mcp_scope["root_path"] = f"{scope.get('root_path', '').rstrip('/')}/mcp"
        await streamable_http.handle_request(mcp_scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with streamable_http.run():
            yield

    async def correction(request: Request) -> HTMLResponse:
        return HTMLResponse(_render_correction_page())

    middleware = [
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]),
        Middleware(_BearerAuthMiddleware),
    ]
    return Starlette(routes=[Route("/correction", correction), Mount("/", app=handle_mcp)], middleware=middleware, lifespan=lifespan)


def main() -> None:
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8080"))
    ssl_certfile = os.environ.get("MCP_SSL_CERTFILE")
    ssl_keyfile = os.environ.get("MCP_SSL_KEYFILE")

    uvicorn_kwargs: dict[str, Any] = {"host": host, "port": port}
    if ssl_certfile or ssl_keyfile:
        missing = [name for name, val in (("MCP_SSL_CERTFILE", ssl_certfile), ("MCP_SSL_KEYFILE", ssl_keyfile)) if not val]
        if missing:
            raise RuntimeError(f"TLS misconfigured — missing variables: {', '.join(missing)}")
        for label, path in (("MCP_SSL_CERTFILE", ssl_certfile), ("MCP_SSL_KEYFILE", ssl_keyfile)):
            if not Path(path).exists():
                raise RuntimeError(f"TLS misconfigured — file not found: {label}={path}")
        uvicorn_kwargs["ssl_certfile"] = ssl_certfile
        uvicorn_kwargs["ssl_keyfile"] = ssl_keyfile
        print(f"[document-mcp] HTTPS enabled — cert={ssl_certfile}")
    else:
        print(f"[document-mcp] HTTP (no TLS)")

    scheme = "https" if ssl_certfile else "http"
    _DOCUMENT_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    _DOCUMENT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[document-mcp] Streamable HTTP on {scheme}://{host}:{port}/mcp")
    uvicorn.run(create_starlette_app(), **uvicorn_kwargs)


if __name__ == "__main__":
    main()

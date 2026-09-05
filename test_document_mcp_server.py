import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TextContent:
    type: str
    text: str


class Tool:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Server:
    def __init__(self, *_args, **_kwargs):
        pass

    def list_tools(self):
        return lambda fn: fn

    def call_tool(self):
        return lambda fn: fn


def install_stubs():
    modules = {
        "mcp": types.ModuleType("mcp"),
        "mcp.server": types.ModuleType("mcp.server"),
        "mcp.server.streamable_http_manager": types.ModuleType("mcp.server.streamable_http_manager"),
        "mcp.types": types.ModuleType("mcp.types"),
    }
    modules["mcp.server"].Server = Server
    modules["mcp.server.streamable_http_manager"].StreamableHTTPSessionManager = object
    modules["mcp.types"].TextContent = TextContent
    modules["mcp.types"].Tool = Tool
    sys.modules.update(modules)
    for name in [
        "starlette.applications",
        "starlette.middleware",
        "starlette.middleware.base",
        "starlette.middleware.cors",
        "starlette.requests",
        "starlette.responses",
        "starlette.routing",
        "starlette.types",
        "uvicorn",
    ]:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["starlette.applications"].Starlette = object
    sys.modules["starlette.middleware"].Middleware = lambda *args, **kwargs: (args, kwargs)
    sys.modules["starlette.middleware.base"].BaseHTTPMiddleware = object
    sys.modules["starlette.middleware.cors"].CORSMiddleware = object
    sys.modules["starlette.requests"].Request = object
    sys.modules["starlette.responses"].HTMLResponse = object
    sys.modules["starlette.responses"].PlainTextResponse = object
    sys.modules["starlette.routing"].Mount = object
    sys.modules["starlette.routing"].Route = object
    sys.modules["starlette.types"].Receive = object
    sys.modules["starlette.types"].Scope = dict
    sys.modules["starlette.types"].Send = object
    sys.modules["uvicorn"].run = lambda *args, **kwargs: None


def load_module(input_dir, output_dir, workspaces_root):
    install_stubs()
    os.environ["DOCUMENT_INPUT_DIR"] = str(input_dir)
    os.environ["DOCUMENT_OUTPUT_DIR"] = str(output_dir)
    os.environ["WORKSPACES_ROOT"] = str(workspaces_root)
    path = Path(__file__).with_name("document_mcp_server.py")
    spec = importlib.util.spec_from_file_location("document_mcp_server_test_subject", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DocumentMcpServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.input_dir = root / "input"
        self.output_dir = root / "output"
        self.workspaces_root = root / "workspaces"
        self.input_dir.mkdir()
        self.output_dir.mkdir()
        self.workspaces_root.mkdir()
        self.server = load_module(self.input_dir, self.output_dir, self.workspaces_root)

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, result):
        return json.loads(result[0].text)

    def test_text_conversion_job_completes_and_status_returns_result(self):
        source = self.input_dir / "note.txt"
        source.write_text("hello\nworld\n", encoding="utf-8")

        start = self.payload(self.server._tool_convert_to_markdown({"filePath": "note.txt"}))
        self.assertTrue(start["ok"])
        job_id = start["jobId"]

        status = {}
        for _ in range(50):
            status = self.payload(self.server._tool_conversion_status({"jobId": job_id}))
            if status["_activity"]["status"] != "running":
                break
            time.sleep(0.02)

        self.assertTrue(status["ok"])
        self.assertEqual(status["_activity"]["status"], "done")
        self.assertIn("hello", status["markdown"])
        self.assertNotIn("polish", status["markdown"])
        self.assertTrue(Path(status["outputPath"]).is_file())

    def test_image_conversion_continues_when_llm_ocr_is_unavailable(self):
        self.server._LLM_API_KEY = ""
        source = self.input_dir / "scan.png"
        source.write_bytes(b"not really an image but enough for fallback test")

        start = self.payload(self.server._tool_convert_to_markdown({"filePath": "scan.png"}))
        self.assertTrue(start["ok"])
        job_id = start["jobId"]

        status = {}
        for _ in range(50):
            status = self.payload(self.server._tool_conversion_status({"jobId": job_id}))
            if status["_activity"]["status"] != "running":
                break
            time.sleep(0.02)

        self.assertTrue(status["ok"])
        self.assertEqual(status["_activity"]["status"], "done")
        self.assertEqual(status["method"], "image-fallback")
        self.assertIn("skipped", status["ocr"])
        self.assertIn("ocr: \"skipped", status["markdown"])
        self.assertNotIn("polish", status["markdown"])
        self.assertTrue(Path(status["outputPath"]).is_file())

    def test_looks_degenerate_flags_repeated_line(self):
        repeated = "\n\n".join(["# 12/18"] * 12)
        self.assertTrue(self.server._looks_degenerate(repeated))

        varied = "\n\n".join(f"line {i}" for i in range(12))
        self.assertFalse(self.server._looks_degenerate(varied))

    def test_looks_degenerate_tolerates_legitimate_repeated_footers(self):
        # A footer table legitimately repeats on every page (17 times here) —
        # that is not the runaway repetition the guard exists to catch.
        footer_lines = ["Titre : ACPI III – Cahier des charges", "Référence :", "Date d'application : 30/07/2026"]
        lines = footer_lines * 17 + [f"paragraphe {i}" for i in range(600)]
        self.assertFalse(self.server._looks_degenerate("\n\n".join(lines)))

        # A runaway still trips it: the same line dominates the document.
        self.assertTrue(self.server._looks_degenerate("\n\n".join(["12/18"] * 100 + ["unique"])))

    def test_polish_markdown_falls_back_on_degenerate_output(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "mistral-small-3-2-24b-instruct-2506"

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                body = {"choices": [{"message": {"content": "\n\n".join(["12/18"] * 50)}}]}
                return json.dumps(body).encode("utf-8")

        original_urlopen = self.server.urllib.request.urlopen
        self.server.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server.urllib.request.urlopen = original_urlopen

        self.assertEqual(polished, "# raw\n\ntext")
        self.assertIn("skipped", reason)
        self.assertIn("degenerate", reason)

    def test_polish_markdown_does_not_send_max_tokens(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-model"

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                body = {"choices": [{"message": {"content": "# cleaned"}}]}
                return json.dumps(body).encode("utf-8")

        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        original_urlopen = self.server.urllib.request.urlopen
        self.server.urllib.request.urlopen = fake_urlopen
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server.urllib.request.urlopen = original_urlopen

        self.assertEqual(polished, "# cleaned")
        self.assertEqual(reason, "done")
        self.assertNotIn("max_tokens", captured["payload"])

    def test_polish_markdown_falls_back_on_context_length_error(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-small-context-model"

        def fake_urlopen(request, timeout=None):
            body = json.dumps({
                "detail": {"error": {"message": (
                    "This model's maximum context length is 16384 tokens. However, you "
                    "requested 20000 output tokens and your prompt contains at least 2150 "
                    "input tokens."
                )}}
            }).encode("utf-8")
            raise self.server.urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(body))

        original_urlopen = self.server.urllib.request.urlopen
        self.server.urllib.request.urlopen = fake_urlopen
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server.urllib.request.urlopen = original_urlopen

        self.assertEqual(polished, "# raw\n\ntext")
        self.assertIn("skipped", reason)

    def test_polish_markdown_falls_back_when_output_is_truncated(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-small-context-model"

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                body = {"choices": [{"finish_reason": "length", "message": {"content": "# cut off"}}]}
                return json.dumps(body).encode("utf-8")

        original_urlopen = self.server.urllib.request.urlopen
        self.server.urllib.request.urlopen = lambda *args, **kwargs: FakeResponse()
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server.urllib.request.urlopen = original_urlopen

        self.assertEqual(polished, "# raw\n\ntext")
        self.assertIn("truncated", reason)

    def test_polish_markdown_skipped_without_llm_configured(self):
        self.server._LLM_API_KEY = ""
        polished, reason = self.server._polish_markdown("# raw\n\ntext")
        self.assertEqual(polished, "# raw\n\ntext")
        self.assertIsNone(reason)

    def test_polish_markdown_falls_back_when_llm_call_fails(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "mistral-small-3-2-24b-instruct-2506"

        def failing_call(_markdown, **kwargs):
            raise ValueError("boom")

        original = self.server._llm_polish_call
        self.server._llm_polish_call = failing_call
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server._llm_polish_call = original

        self.assertEqual(polished, "# raw\n\ntext")
        self.assertIn("skipped", reason)

    def test_polish_markdown_returns_cleaned_content_on_success(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "mistral-small-3-2-24b-instruct-2506"

        original = self.server._llm_polish_call
        self.server._llm_polish_call = lambda _markdown, **kwargs: "# cleaned\n\ntext"
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server._llm_polish_call = original

        self.assertEqual(polished, "# cleaned\n\ntext")
        self.assertEqual(reason, "done")

    def test_gpt5_models_omit_temperature_from_llm_payloads(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "gpt-5.4-mini"

        captured = {}

        class FakeResponse:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return json.dumps({"choices": [{"message": {"content": "# cleaned"}}]}).encode("utf-8")

        def fake_urlopen(request, timeout=None):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse()

        original_urlopen = self.server.urllib.request.urlopen
        self.server.urllib.request.urlopen = fake_urlopen
        try:
            polished, reason = self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server.urllib.request.urlopen = original_urlopen

        self.assertEqual(polished, "# cleaned")
        self.assertEqual(reason, "done")
        self.assertNotIn("temperature", captured["payload"])

        self.server._LLM_MODEL = "mistral-small-3-2-24b-instruct-2506"
        captured.clear()
        original_urlopen = self.server.urllib.request.urlopen
        self.server.urllib.request.urlopen = fake_urlopen
        try:
            self.server._polish_markdown("# raw\n\ntext")
        finally:
            self.server.urllib.request.urlopen = original_urlopen
        self.assertEqual(captured["payload"]["temperature"], 0)

    def test_model_refuses_temperature_matches_gpt5_class_names(self):
        self.assertTrue(self.server._model_refuses_temperature("gpt-5.4-mini"))
        self.assertTrue(self.server._model_refuses_temperature("openai/gpt-5-mini"))
        self.assertTrue(self.server._model_refuses_temperature("gpt-5.2"))
        self.assertFalse(self.server._model_refuses_temperature("gpt-4o"))
        self.assertFalse(self.server._model_refuses_temperature("mistral-small-3-2-24b-instruct-2506"))

    def test_wikirc_llm_block_reads_llm_section_only(self):
        ws = self.workspaces_root / "acpi"
        ws.mkdir()
        (ws / ".wikirc.yaml").write_text(
            "language: fr\n"
            "llm:\n"
            "  provider: openai-compatible\n"
            "  engine: albert\n"
            "  baseUrl: https://albert.api.etalab.gouv.fr/v1\n"
            "  model: openai/gpt-oss-120b\n"
            "  apiKey: sk-SECRET\n"
            "retrieval:\n"
            "  vector:\n"
            "    baseUrl: https://other.example/v1\n",
            encoding="utf-8",
        )
        block = self.server._wikirc_llm_block(ws)
        self.assertIsNotNone(block)
        self.assertEqual(block["model"], "openai/gpt-oss-120b")
        self.assertEqual(block["baseUrl"], "https://albert.api.etalab.gouv.fr/v1")
        self.assertEqual(block["apiKey"], "sk-SECRET")
        self.assertNotIn("vector", block)
        self.assertNotIn("provider", block.get("vector", {}))

        (ws / ".wikirc.yaml").write_text("llm:\n  model: only-model\n", encoding="utf-8")
        self.assertIsNone(self.server._wikirc_llm_block(ws))
        self.assertIsNone(self.server._wikirc_llm_block(None))

    def test_polish_uses_workspace_wikirc_llm(self):
        ws = self.workspaces_root / "acpi"
        ws.mkdir()
        (ws / ".wikirc.yaml").write_text(
            "llm:\n"
            "  baseUrl: https://albert.api.etalab.gouv.fr/v1\n"
            "  model: openai/gpt-oss-120b\n"
            "  apiKey: sk-WIKIRC\n",
            encoding="utf-8",
        )
        captured = {}

        def fake_call(markdown, language=None, base_url=None, model=None, api_key=None):
            captured.update(base_url=base_url, model=model, api_key=api_key)
            return "# cleaned"

        original = self.server._llm_polish_call
        self.server._llm_polish_call = fake_call
        try:
            polished, reason = self.server._polish_markdown("body", "fr", ws)
        finally:
            self.server._llm_polish_call = original

        self.assertEqual(polished, "# cleaned")
        self.assertEqual(reason, "done")
        self.assertEqual(captured["model"], "openai/gpt-oss-120b")
        self.assertEqual(captured["base_url"], "https://albert.api.etalab.gouv.fr/v1")
        self.assertEqual(captured["api_key"], "sk-WIKIRC")

    def test_polish_falls_back_to_documents_llm_without_workspace(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-model"
        self.assertEqual(
            self.server._polish_llm_config(None),
            (self.server._LLM_BASE_URL, "some-model", "sk-test"),
        )
        self.server._LLM_API_KEY = ""
        self.assertIsNone(self.server._polish_llm_config(None))

    def test_workspace_language_reads_top_level_wikirc_key(self):
        self.assertIsNone(self.server._workspace_language(None))
        self.assertIsNone(self.server._workspace_language(self.workspaces_root))

        ws = self.workspaces_root / "acpi"
        ws.mkdir()
        (ws / ".wikirc.yaml").write_text("language: fr\nllm:\n  apiKey: sk-SECRET\n", encoding="utf-8")
        self.assertEqual(self.server._workspace_language(ws), "fr")

        (ws / ".wikirc.yaml").write_text("llm:\n  language: nested-not-read\nlanguage: de\n", encoding="utf-8")
        self.assertEqual(self.server._workspace_language(ws), "de")

    def test_polish_prompt_mentions_workspace_language_and_link_repair(self):
        prompt = self.server._polish_prompt("fr")
        self.assertIn("fr", prompt)
        self.assertIn("links", prompt)
        self.assertIn("do not translate", prompt)

        no_language = self.server._polish_prompt(None)
        self.assertNotIn("do not translate", no_language)

    def test_polished_conversion_skips_text_and_fallbacks(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-model"

        def failing_call(_markdown, **kwargs):
            raise ValueError("boom")

        original = self.server._llm_polish_call
        self.server._llm_polish_call = failing_call
        try:
            markdown, polish_status = self.server._polish_converted_markdown("body", "text", None)
            self.assertEqual(markdown, "body")
            self.assertIsNone(polish_status)

            markdown, polish_status = self.server._polish_converted_markdown("stub", "image-fallback", None)
            self.assertEqual(markdown, "stub")
            self.assertIsNone(polish_status)

            markdown, polish_status = self.server._polish_converted_markdown("stub", "pdf-fallback", None)
            self.assertEqual(markdown, "stub")
            self.assertIsNone(polish_status)

            markdown, polish_status = self.server._polish_converted_markdown("body", "pdf-markitdown", None)
            self.assertEqual(markdown, "body")
            self.assertIn("skipped", polish_status)
        finally:
            self.server._llm_polish_call = original

    def test_with_metadata_includes_polish_status(self):
        source = self.input_dir / "note.pdf"
        source.write_bytes(b"%PDF")
        text = self.server._with_metadata("body", source, "pdf-llm-ocr", "done", "skipped (boom)")
        self.assertIn('polish: "skipped (boom)"', text)
        self.assertIn('ocr: "done"', text)

        text = self.server._with_metadata("body", source, "pdf-markitdown", None, "done")
        self.assertIn('polish: "done"', text)
        self.assertNotIn("ocr:", text)

    def test_split_markdown_chunks_respects_size_cap(self):
        sections = [f"## Section {i}\n\n" + ("paragraph content " * 400) for i in range(6)]
        markdown = "\n\n".join(sections)
        chunks = self.server._split_markdown_chunks(markdown)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= self.server._POLISH_CHUNK_MAX_CHARS for chunk in chunks))

        small = "# Title\n\nsome text"
        chunks = self.server._split_markdown_chunks(small)
        self.assertEqual(len(chunks), 1)

    def test_polish_markdown_retries_chunked_when_output_truncates(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-model"
        sections = [f"## Section {i}\n\n" + ("paragraph content " * 200) for i in range(8)]
        markdown = "\n\n".join(sections)

        calls = []

        def chunked_call(content, **kwargs):
            calls.append(content)
            if content is markdown:
                raise self.server._PolishSizeError("truncated")
            return f"cleaned:{len(content)}"

        original = self.server._llm_polish_call
        self.server._llm_polish_call = chunked_call
        try:
            polished, reason = self.server._polish_markdown(markdown)
        finally:
            self.server._llm_polish_call = original

        self.assertEqual(reason, "done")
        self.assertEqual(len(calls), 1 + len(self.server._split_markdown_chunks(markdown)))
        self.assertIn("cleaned:", polished)

    def test_polish_chunks_partial_failure_keeps_original_chunk(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-model"

        original = self.server._llm_polish_call

        def flaky(content, **kwargs):
            if content == "first chunk":
                raise ValueError("boom")
            return "CLEANED " + content

        self.server._llm_polish_call = flaky
        try:
            polished, reason = self.server._polish_chunks(
                ["first chunk", "second chunk"], None, "https://x/v1", "m", "k"
            )
        finally:
            self.server._llm_polish_call = original

        self.assertIn("first chunk", polished)
        self.assertIn("CLEANED second chunk", polished)
        self.assertIn("partial (1/2", reason)

    def test_polish_chunks_all_failures_return_original_with_skipped(self):
        self.server._LLM_API_KEY = "sk-test"
        self.server._LLM_MODEL = "some-model"

        original = self.server._llm_polish_call

        def failing(content, **kwargs):
            raise ValueError("boom")

        self.server._llm_polish_call = failing
        try:
            polished, reason = self.server._polish_chunks(
                ["chunk one", "chunk two"], None, "https://x/v1", "m", "k"
            )
        finally:
            self.server._llm_polish_call = original

        self.assertIn("chunk one", polished)
        self.assertIn("chunk two", polished)
        self.assertIn("skipped", reason)

    def test_cleanup_markdown_converts_html_and_fixes_indented_headings(self):
        dirty = (
            "<div style=\"text-align: center;\">\n"
            "  <h2>Boîte</h2>\n"
            "  <div style=\"border: 2px solid #d32f2f;\">Racine du compte</div>\n"
            "</div>\n"
            "  # Titre indenté\n"
            "<table border=\"1\"><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>\n"
        )
        clean = self.server._cleanup_markdown(dirty)
        self.assertNotIn("<div", clean)
        self.assertNotIn("<table", clean)
        self.assertNotIn("  # Titre", clean)
        self.assertIn("# Titre indenté", clean)
        self.assertIn("| A | B |", clean)
        self.assertIn("| --- | --- |", clean)
        self.assertIn("| 1 | 2 |", clean)
        self.assertIn("Racine du compte", clean)

    def test_cleanup_markdown_drops_local_image_refs_and_keeps_urls(self):
        text = "avant\n![image](image_1.png)\naprès\n![schéma](https://example.com/a.png)\n"
        clean = self.server._cleanup_markdown(text)
        self.assertNotIn("image_1.png", clean)
        self.assertIn("https://example.com/a.png", clean)
        self.assertIn("avant", clean)
        self.assertIn("après", clean)

    def test_cleanup_markdown_rejoins_split_links(self):
        text = "voir [la doc](https://example.com/page\n/annexe) pour plus"
        clean = self.server._cleanup_markdown(text)
        self.assertIn("[la doc](https://example.com/page/annexe)", clean)

    def test_cleanup_markdown_protects_fenced_blocks(self):
        text = "## Diagramme\n\n```mermaid\nflowchart LR\n  A[\"A <br/> B\"] --> C\n```\n\n<div>reste</div>\n"
        clean = self.server._cleanup_markdown(text)
        self.assertIn('A["A <br/> B"] --> C', clean)
        self.assertNotIn("<div>", clean)
        self.assertIn("reste", clean)

    def test_unknown_job_and_redaction(self):
        status = self.payload(self.server._tool_conversion_status({"jobId": "missing"}))
        self.assertFalse(status["ok"])
        self.assertIn("Unknown job", status["error"])

        masked = self.server._mask_secret_text("Authorization: Bearer abc api_key=secret token:tok123")
        self.assertNotIn("abc", masked)
        self.assertNotIn("secret", masked)
        self.assertNotIn("tok123", masked)

    def test_read_scope_cannot_start_conversion(self):
        token = self.server._CURRENT_SCOPES.set({"read"})
        try:
            denied = self.server._require_tool_scope("documents_convert_to_markdown")
            allowed = self.server._require_tool_scope("documents_status")
        finally:
            self.server._CURRENT_SCOPES.reset(token)

        self.assertFalse(self.payload(denied)["ok"])
        self.assertIn("write scope", self.payload(denied)["error"])
        self.assertIsNone(allowed)

    def test_workspace_input_is_confined_to_staging_area(self):
        (self.workspaces_root / "acpi").mkdir()
        ws = self.server._validate_workspace("acpi")
        staging = ws / "raw" / "untracked"
        staging.mkdir(parents=True, exist_ok=True)
        (ws / ".wikirc.yaml").write_text(
            "llm:\n  apiKey: sk-SECRET\nmcp:\n  accessKey: fa483c\n", encoding="utf-8"
        )
        doc = staging / "note.txt"
        doc.write_text("hello\n", encoding="utf-8")

        # The workspace root (where .wikirc.yaml holds secrets) is not a valid
        # input location, even though .yaml is a supported text extension.
        with self.assertRaises(ValueError):
            self.server._resolve_source({"filePath": ".wikirc.yaml"}, self.output_dir, ws)

        # A file staged under raw/untracked stays resolvable.
        resolved = self.server._resolve_source(
            {"filePath": "raw/untracked/note.txt"}, self.output_dir, ws
        )
        self.assertEqual(str(resolved), str(doc.resolve()))


if __name__ == "__main__":
    unittest.main()

import importlib.util
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
        self.assertTrue(Path(status["outputPath"]).is_file())

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

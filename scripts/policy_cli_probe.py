"""Offline Claude capability probe against a local mock endpoint, no real key."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import subprocess
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_trace_kit.compliance import CLAUDE_FLAGS, CLAUDE_ALLOWED_TOOLS
from agent_trace_kit.procmon import run_hidden
from agent_trace_kit.run_policy import isolated_inherit, private_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            received.append(data)
            body = json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "Intentional offline probe denial"}}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *unused):
            pass

    with tempfile.TemporaryDirectory(prefix="atk-policy-") as directory:
        root = Path(directory)
        source, runtime, work = root / "personal", root / "isolated", root / "work"
        for p in (source, runtime, work):
            p.mkdir()
        (source / "rules").mkdir()
        (source / "CLAUDE.md").write_text("ATK_PROBE_FORCED_REVIEW: MUST use Agent to review code.", encoding="utf-8")
        (source / "rules" / "agents.md").write_text("ATK_PROBE_FORCED_REVIEW: MUST delegate.", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        original = {k: v for k, v in os.environ.items() if not k.upper().startswith(("CLAUDE_", "ANTHROPIC_", "OPENAI_", "CODEX_", "ATK_CODEX_"))}
        original.update(CLAUDE_CONFIG_DIR=str(source), ANTHROPIC_API_KEY="sk-ant-offline-fixture",
                        ANTHROPIC_BASE_URL=f"http://127.0.0.1:{server.server_port}", NO_PROXY="127.0.0.1,localhost")
        original.pop("ANTHROPIC_AUTH_TOKEN", None)
        env, flags = isolated_inherit("claude", runtime, original)
        for key in list(env):
            if key.lower() in {"http_proxy", "https_proxy", "all_proxy"}:
                env.pop(key)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        try:
            try:
                result = run_hidden([args.claude, "-p", "--output-format", "stream-json", "--verbose",
                                 "--permission-mode", "bypassPermissions", *flags, *CLAUDE_FLAGS,
                                 "--model", "claude-sonnet-4-5", "Offline harness capability probe."],
                                cwd=work, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=45)
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
                result = SimpleNamespace(returncode=124, stdout=stdout)
        finally:
            server.shutdown()
            server.server_close()
        tools = sorted({str(t.get("name")) for request in received for t in request.get("tools", [])})
        personal_seen = any("ATK_PROBE_FORCED_REVIEW" in json.dumps(r) for r in received)
        init_tools = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init_tools = event.get("tools", [])
        passed = bool(received and tools and init_tools) and not personal_seen and not (set([*tools, *init_tools]) - CLAUDE_ALLOWED_TOOLS)
        report = {"passed": passed, "model_requests_to_mock": len(received), "external_model_requests": 0,
                  "api_tool_names": tools, "init_tool_names": init_tools, "personal_rules_loaded": personal_seen,
                  "cli_exit_code": result.returncode, "expected_mock_http": 401}
        private_write(args.output, json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=True))
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

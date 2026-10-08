"""Project-only credentials, model catalogs, frozen jobs and child isolation."""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from agent_trace_kit import engines
from agent_trace_kit.cli_config import CliConnections, validate_model, validate_url
from agent_trace_kit.desk import DeskServer
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit import workspace as ws
from agent_trace_kit.runner import PairRunner


@pytest.fixture(autouse=True)
def isolated_user_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))


@pytest.fixture
def gateway():
    calls = []
    state = {
        "status": 200,
        "response": {"data": [{"id": "fixture-coder"}, {"id": "fixture-small"}]},
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append({"path": self.path, "headers": dict(self.headers)})
            self.send_response(state["status"])
            if state.get("redirect"):
                self.send_header("Location", state["redirect"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = state["response"]
            if callable(data):
                data = data(self.path)
            self.wfile.write(json.dumps(data).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def connection(
    base="https://provider.example/v1",
    key="fixture-secret-123",
    model="fixture-coder",
    **extra,
):
    return {
        "mode": "project",
        "base_url": base,
        "api_key": key,
        "model": model,
        **extra,
    }


def test_keys_are_write_only_separate_and_protected(tmp_path):
    store = DeskStore(tmp_path / "desk")
    cli = store.cli_connections
    codex = cli.save("codex", connection())
    claude = cli.save("claude", connection(key="fixture-claude-secret", auth="api_key"))
    assert codex["id"] != claude["id"]
    assert "secret" not in codex and "api_key" not in codex
    assert all(p["key_configured"] for p in cli.public().values())
    public = json.dumps({"settings": store.settings(), "profiles": cli.public()})
    assert "fixture-secret-123" not in public
    assert "fixture-claude-secret" not in public
    disk = cli.path.read_text(encoding="utf-8")
    if os.name == "nt":
        assert "fixture-secret-123" not in disk
        assert "fixture-claude-secret" not in disk
        assert "dpapi" in disk
    else:
        assert cli.path.stat().st_mode & 0o777 == 0o600
    assert cli.public()["codex"]["id"] == codex["id"]


def test_explicit_inherit_profile_clears_legacy_default_model(tmp_path):
    store = DeskStore(tmp_path)
    store.save_settings({"codex_model": "legacy-model"})
    assert (
        store.create_job({"prompt": "p", "agent": "codex"})["codex_model"]
        == "legacy-model"
    )
    store.cli_connections.save("codex", {"mode": "inherit", "model": ""})
    assert store.create_job({"prompt": "p", "agent": "codex"})["codex_model"] == ""


def test_private_claude_trace_and_watchdog_handle_long_windows_paths(tmp_path):
    home = tmp_path / "private-cli"
    work = tmp_path / ("workspace" * 12)
    path = ws.native_path(
        home / "projects" / ws._encode_cwd(work) / "session-test.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"type": "user", "sessionId": "session-test", "cwd": str(work.resolve())}
        )
        + "\n",
        encoding="utf-8",
    )
    found = ws.find_session_jsonl(work, config_dir=str(home))
    assert len(found) == 1 and found[0]["session_id"] == "session-test"
    assert (
        PairRunner._latest_transcript_mtime(str(work), config_dir=str(home))
        == path.stat().st_mtime
    )
    assert Path(found[0]["path"]).is_file()


@pytest.mark.parametrize(
    "url",
    [
        "http://provider.example/v1",
        "ftp://provider.example",
        "https://key@provider.example/v1",
        "https://provider.example/v1?api_key=x",
        "https://provider.example/v1#key=x",
        "https://provider.example/v1\r\nX-Key: value",
        "https://provider.example:bad/v1",
        "https://provider.example/v1/responses",
        "https://provider.example/v1/models",
    ],
)
def test_invalid_endpoints_rejected(url):
    with pytest.raises(ValueError):
        validate_url(url)


@pytest.mark.parametrize(
    "model", ["x & echo key", "x\nsecret", "x;echo", "--model=x", "x%SECRET%"]
)
def test_model_id_cannot_be_shell_syntax(model):
    with pytest.raises(ValueError):
        validate_model(model)


def test_key_retained_only_for_same_destination(tmp_path):
    cli = CliConnections(tmp_path)
    cli.save("codex", connection())
    updated = cli.save("codex", {"model": "different-model"})
    assert updated["key_configured"]
    with pytest.raises(ValueError, match="重新填写 Key"):
        cli.save("codex", {"base_url": "https://other.example/v1"})
    assert cli.public()["codex"]["id"] == updated["id"]
    with pytest.raises(ValueError, match="API Key"):
        cli.save("claude", {"mode": "project", "base_url": "https://anthropic.example"})


@pytest.mark.parametrize(
    "agent,auth,path,header",
    [
        ("codex", "bearer", "/v1/models", "Authorization"),
        ("claude", "api_key", "/v1/models", "X-Api-Key"),
        ("claude", "bearer", "/v1/models", "Authorization"),
    ],
)
def test_live_models_use_draft_or_saved_key_without_persisting_query(
    tmp_path, gateway, agent, auth, path, header
):
    base, calls, _ = gateway
    cli = CliConnections(tmp_path)
    draft = connection(base=base + ("/v1" if agent == "codex" else ""), auth=auth)
    result = cli.models(agent, draft)
    assert result == {"models": ["fixture-coder", "fixture-small"], "truncated": False}
    assert not cli.path.exists()
    assert calls[-1]["path"] == path
    assert calls[-1]["headers"][header].endswith("fixture-secret-123")
    if agent == "claude":
        assert calls[-1]["headers"]["Anthropic-Version"] == "2023-06-01"
    cli.save(agent, draft)
    assert cli.models(agent, {}) == result
    with pytest.raises(ValueError, match="重新填写 Key"):
        cli.models(agent, {"base_url": "https://unexpected.example/v1"})
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 403, 404, 500, 302])
def test_query_errors_never_echo_provider_body_or_follow_redirect(
    tmp_path, gateway, status
):
    base, calls, state = gateway
    state.update(
        status=status, response={"error": "fixture-secret-123"}, redirect=base + "/leak"
    )
    with pytest.raises(ValueError) as caught:
        CliConnections(tmp_path).models("codex", connection(base))
    assert str(status) in str(caught.value)
    assert "fixture-secret-123" not in str(caught.value)
    assert len(calls) == 1


def test_model_pages_and_malformed_results(tmp_path, gateway):
    base, calls, state = gateway
    state["response"] = lambda path: (
        {"data": [{"id": "model-two"}], "has_more": False}
        if "after_id=" in path
        else {"data": [{"id": "model-one"}], "has_more": True, "last_id": "model-one"}
    )
    result = CliConnections(tmp_path).models("claude", connection(base))
    assert result["models"] == ["model-one", "model-two"]
    assert calls[-1]["path"] == "/v1/models?after_id=model-one"
    state["response"] = {"message": "fixture-secret-123"}
    with pytest.raises(ValueError, match="格式不支持"):
        CliConnections(tmp_path).models("codex", connection(base))


def test_pair_pins_model_endpoint_and_key_while_new_tasks_pick_up_edits(tmp_path):
    store = DeskStore(tmp_path / "desk")
    cli = store.cli_connections
    first = cli.save("codex", connection())
    job = store.create_job(
        {
            "prompt": "中文任务",
            "agent": "codex",
            "cli_model": "chosen-model",
            "cli_connection_id": first["id"],
        }
    )
    cli.save(
        "codex",
        connection(
            base="https://new.example/v1", key="fixture-new-key", model="new-model"
        ),
    )
    second = store.create_job({"prompt": "p", "agent": "codex"})
    assert job["codex_model"] == "chosen-model"
    assert second["codex_model"] == "new-model"
    inherited = {
        "CODEX_HOME": str(tmp_path / "user-home"),
        "OPENAI_API_KEY": "user-key",
        "ANTHROPIC_AUTH_TOKEN": "unrelated",
        "PATH": "user-path",
    }
    before = dict(inherited)
    env, flags = cli.runtime(job, inherited)
    next_env, _ = cli.runtime(second, inherited)
    assert inherited == before
    assert env["ATK_CODEX_API_KEY"] == "fixture-secret-123"
    assert next_env["ATK_CODEX_API_KEY"] == "fixture-new-key"
    assert env["PATH"] == "user-path"
    assert "ANTHROPIC_AUTH_TOKEN" not in env and "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == job["cli_home"]
    config = (Path(job["cli_home"]) / "config.toml").read_text(encoding="utf-8")
    assert 'base_url = "https://provider.example/v1"' in config
    if os.name == "nt":
        assert '[windows]\nsandbox = "elevated"' in config
    else:
        assert '[windows]' not in config
    assert "fixture-secret-123" not in config
    assert "fixture-secret-123" not in json.dumps(flags)
    assert "fixture-secret-123" not in store.job_path(job["id"]).read_text(
        encoding="utf-8"
    )
    assert not (tmp_path / "user-home").exists()
    with pytest.raises(ValueError, match="配置已变更"):
        store.create_job(
            {"prompt": "p", "agent": "codex", "cli_connection_id": first["id"]}
        )


@pytest.mark.parametrize("base_suffix", ["", "/v1"])
def test_claude_connection_is_separate_and_inherit_mode_remains_available(
    tmp_path, base_suffix
):
    store = DeskStore(tmp_path)
    store.cli_connections.save("codex", connection())
    store.cli_connections.save(
        "claude",
        connection(
            base="https://claude.example" + base_suffix,
            key="claude-only-key",
            model="vendor/claude",
            auth="bearer",
        ),
    )
    job = store.create_job({"prompt": "p", "agent": "claude"})
    env, flags = store.cli_connections.runtime(
        job, {"ANTHROPIC_API_KEY": "wrong", "OPENAI_API_KEY": "wrong"}
    )
    assert env["ANTHROPIC_AUTH_TOKEN"] == "claude-only-key"
    assert env["ANTHROPIC_BASE_URL"] == "https://claude.example"
    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["CLAUDE_CONFIG_DIR"] == job["cli_home"]
    assert flags == ["--setting-sources", ""]
    assert job["claude_model"] == "vendor/claude" and job["codex_model"] == ""
    store.cli_connections.save("claude", {"mode": "inherit", "model": ""})
    fresh = store.create_job({"prompt": "p", "agent": "claude"})
    original = {"ANTHROPIC_AUTH_TOKEN": "global-auth"}
    env, flags = store.cli_connections.runtime(fresh, original)
    assert env["ANTHROPIC_AUTH_TOKEN"] == "global-auth"
    assert env["CLAUDE_CONFIG_DIR"] == fresh["cli_home"]
    assert original == {"ANTHROPIC_AUTH_TOKEN": "global-auth"}
    assert flags == ["--setting-sources", ""]
    assert store.cli_connections.public()["codex"]["mode"] == "project"


def test_batch_model_override_and_query_requires_project_mode(tmp_path, monkeypatch):
    server = DeskServer(DeskStore(tmp_path / "desk"))
    server.store.cli_connections.save("codex", connection())
    monkeypatch.setattr(server.runner, "prepare", lambda _: {"baseline": {}})
    result = server.job_batch(
        {"lines": "任务\t\t\t\tnew-repo\tcodex\t\t\tchosen-batch-model"}
    )
    assert not result["errors"]
    assert (
        server.store.get_job(result["created"][0])["codex_model"]
        == "chosen-batch-model"
    )
    with pytest.raises(ValueError, match="专用连接"):
        server.store.cli_connections.models("claude", {})


def test_recollection_uses_frozen_cli_home_after_configuration_changes(
    tmp_path, monkeypatch
):
    store = DeskStore(tmp_path / "desk")
    store.cli_connections.save("codex", connection())
    job = store.create_job({"prompt": "p", "agent": "codex"})
    recorded = {}

    def find(workspace, **kwargs):
        recorded.update(kwargs)
        return []

    monkeypatch.setattr(engines, "find_codex_sessions", find)
    store.cli_connections.save("codex", {"mode": "inherit"})
    engines.find_job_sessions(job, tmp_path, {"env_overrides": {"CODEX_HOME": "wrong"}})
    assert recorded["env"]["CODEX_HOME"] == job["cli_home"]


def test_task_runtime_path_is_child_only_and_preserves_original(tmp_path):
    cli = CliConnections(tmp_path / "desk")
    binary_dir = tmp_path / "python"
    binary_dir.mkdir()
    original = {"Path": "existing"}
    job = {"runtime_path_prepend": [str(binary_dir)]}
    env, _ = cli.runtime(job, original)
    assert env["Path"] == str(binary_dir) + os.pathsep + "existing"
    assert original == {"Path": "existing"}
    with pytest.raises(ValueError, match="PATH"):
        cli.runtime({"runtime_path_prepend": [str(tmp_path / "missing")]}, original)

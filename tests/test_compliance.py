"""Formal-run policy regressions; synthetic evidence, no model requests."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_trace_kit import compliance as c, engines, run_policy, workspace
from agent_trace_kit.cli_config import CliConnections
from agent_trace_kit.desk import DeskServer
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.export_tsv import export_tsv


def write(path, rows):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def claude(tool="Agent", receipt=None, error=False):
    return [
        {"type": "user", "sessionId": "s", "message": {"content": "实现模块"}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "call", "name": tool, "input": {"prompt": "Review code"}}]}},
        {"type": "user", "toolUseResult": receipt or {"status": "async_launched", "agentId": "child"},
         "message": {"content": [{"type": "tool_result", "tool_use_id": "call", "is_error": error, "content": "receipt"}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "review still running"}]}},
    ]


def codex(tool="spawn_agent", output=None):
    return [
        {"type": "session_meta", "payload": {"id": "s", "cwd": "work"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": "实现模块"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": tool, "call_id": "call", "arguments": '{}'}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call", "output": json.dumps(output or {"agent_id": "child", "status": "running"})}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "review running"}]}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]


@pytest.mark.parametrize("factory,tool", [(claude, "Agent"), (claude, "Task"), (codex, "spawn_agent"), (codex, "collaboration.spawn_agent")])
def test_actual_dispatch_blocks_without_report_and_keeps_raw(tmp_path, factory, tool):
    path = write(tmp_path / "raw.jsonl", factory(tool))
    original = path.read_bytes()
    report = c.audit_trace(path)
    assert report["status"] == "issue" and report["reason"] == "extra_ai_dispatched"
    assert report["sha256"] == hashlib.sha256(original).hexdigest()
    assert report["calls"][0]["report_line"] is None
    assert report["calls"][0]["result_line"] in {3, 4}
    reason = engines.codex_evidence(path)["reason"] if factory == codex else workspace.transcript_interruption_reason(path)
    assert reason == "extra_ai_dispatched"
    assert path.read_bytes() == original


@pytest.mark.parametrize("factory", [claude, codex])
def test_permission_denial_is_recorded_not_a_successful_dispatch(tmp_path, factory):
    rows = factory(receipt={"error": "permission denied"}, error=True) if factory == claude else factory(output={"error": "blocked by policy"})
    report = c.audit_trace(write(tmp_path / "raw.jsonl", rows))
    assert report["status"] == "clear" and report["reason"] is None
    assert report["calls"][0]["status"] == "blocked"


@pytest.mark.parametrize("tool", ["TaskCreate", "TaskUpdate", "TaskList", "Bash"])
def test_local_tracking_and_business_agent_strings_are_not_delegation(tmp_path, tool):
    rows = claude(tool)
    rows[1]["message"]["content"][0]["input"] = {"command": "python -m simulated_agent --test"}
    assert c.audit_trace(write(tmp_path / "raw.jsonl", rows))["calls"] == []


@pytest.mark.parametrize("wrapper", ["queued", "user"])
def test_substantive_async_report_in_both_real_notification_wrappers(tmp_path, wrapper):
    text = '<task-notification><tool-use-id>call</tool-use-id><status>completed</status><result>Found race, add a lock.</result></task-notification>'
    notification = {"type": "attachment", "attachment": {"type": "queued_command", "prompt": text}} if wrapper == "queued" else {"type": "user", "message": {"content": text}}
    report = c.audit_trace(write(tmp_path / "raw.jsonl", [*claude(), notification]))
    assert report["calls"][0]["report_line"] == 5 and report["reason"] == "extra_ai_dispatched"


def test_missing_receipt_is_pending_not_clear_or_proven_dispatched(tmp_path):
    rows = codex()
    del rows[3]
    report = c.audit_trace(write(tmp_path / "raw.jsonl", rows))
    assert report["status"] == "needs_review"
    assert report["calls"][0]["status"] == "unresolved"


def test_api_failure_does_not_prove_permission_denial(tmp_path):
    rows = claude(receipt={"error": "HTTP 504"}, error=True)
    assert c.audit_trace(write(tmp_path / "raw.jsonl", rows))["status"] == "needs_review"


def test_terminated_agent_still_had_a_dispatch(tmp_path):
    rows = claude(receipt={"error": "Agent terminated due to HTTP 504"}, error=True)
    assert c.audit_trace(write(tmp_path / "raw.jsonl", rows))["reason"] == "extra_ai_dispatched"


@pytest.mark.parametrize("raw", ['{}\n{broken', '[]\n', '\ufffd\n'])
def test_malformed_raw_evidence_fails_closed(tmp_path, raw):
    path = tmp_path / "raw.jsonl"
    path.write_text(raw, encoding="utf-8")
    report = c.audit_trace(path)
    assert report["status"] == "not_verified" and report["reason"] == "malformed_json"


def test_tool_catalog_and_considered_review_are_not_actual_calls(tmp_path):
    rows = [{"type": "system", "tools": ["Agent"]}, {"type": "assistant", "message": {"content": [{"type": "text", "text": "Could spawn_agent, but I will review myself."}]}}]
    assert c.audit_trace(write(tmp_path / "raw.jsonl", rows))["status"] == "clear"


@pytest.mark.parametrize("agent", ["codex", "claude"])
def test_inherit_only_auth_and_provider_never_personal_agent_instructions(tmp_path, agent):
    source, home = tmp_path / "personal", tmp_path / "isolated"
    source.mkdir()
    home.mkdir()
    (source / "agents").mkdir()
    (source / "AGENTS.md").write_text("MUST delegate to a reviewer", encoding="utf-8")
    env = {"PATH": "python-bin", "CODEX_HOME": str(source), "CLAUDE_CONFIG_DIR": str(source), "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}
    if agent == "codex":
        (source / "config.toml").write_text('model = "test-model"\nmodel_provider = "vendor"\ndeveloper_instructions = "delegate"\n[features]\nmulti_agent = true\n[model_providers.vendor]\nname = "Test"\nbase_url = "https://example.invalid/v1"\nenv_key = "TEST_KEY"\n[mcp_servers.extra]\ncommand = "helper"\n', encoding="utf-8")
        (source / "auth.json").write_text('{"tokens":{"access_token":"fixture"}}', encoding="utf-8")
        env["TEST_KEY"] = "fixture-key"
    else:
        (source / "settings.json").write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "fixture-key", "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}, "hooks": {"delegate": True}}), encoding="utf-8")
    original = dict(env)
    child, _ = run_policy.isolated_inherit(agent, home, env)
    assert env == original and child["PATH"] == "python-bin"
    assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in child
    assert not (home / "agents").exists() and not (home / "AGENTS.md").exists()
    if agent == "codex":
        config = (home / "config.toml").read_text(encoding="utf-8")
        assert "delegate" not in config and "mcp_servers" not in config and "features" not in config
        assert child["TEST_KEY"] == "fixture-key" and child["CODEX_HOME"] == str(home)
    else:
        assert child["ANTHROPIC_AUTH_TOKEN"] == "fixture-key" and child["CLAUDE_CONFIG_DIR"] == str(home)
        assert not (home / "settings.json").exists()


@pytest.mark.parametrize("name", ["agents", "AGENTS.md", "claude.md", "plugins", "skills", "settings.json"])
def test_contaminated_runtime_home_is_rejected_without_deletion(tmp_path, name):
    (tmp_path / name).write_text("private instructions", encoding="utf-8")
    with pytest.raises(ValueError, match="个人指令"):
        run_policy.check_home(tmp_path)
    assert (tmp_path / name).exists()


@pytest.mark.parametrize("features,ok", [("multi_agent stable false\nmulti_agent_v2 stable false\n", True), ("multi_agent stable true\nmulti_agent_v2 stable false\n", False), ("multi_agent stable false\n", False)])
def test_preflight_checks_effective_features_and_fails_on_unknown(tmp_path, monkeypatch, features, ok):
    from agent_trace_kit import procmon
    captured = []
    def run(args, **kwargs):
        captured.extend(args)
        return SimpleNamespace(returncode=0, stdout=features)
    monkeypatch.setattr(procmon, "run_hidden", run)
    monkeypatch.setattr(run_policy, "_codex_skills", lambda *args: {"skills_check": "passed"})
    if ok:
        assert run_policy.preflight("codex", "codex", {"CODEX_HOME": str(tmp_path)}, [], tmp_path)["capability_check"] == "passed"
    else:
        with pytest.raises(ValueError, match="拒绝正式运行"):
            run_policy.preflight("codex", "codex", {}, [])
    assert "exec" not in captured and "features.multi_agent=false" in captured


def valid_job(store, tmp_path, rows):
    job = store.create_job({"prompt": "Windows 本地运行", "agent": "codex"})
    for name in ("A", "B"):
        path = write(tmp_path / f"{name}.jsonl", rows)
        store.update_side(job["id"], name, {"jsonl_local": str(path), "session_id": "s", "status": "done"})
    store.update_review(job["id"], {"validity": "有效", "conclusion": "A 更好", "reason": "A 的错误路径测试通过；B 仍失败。", "a_delivery_score": "5", "a_delivery_description": "完成", "b_delivery_score": "4", "b_delivery_description": "有缺陷"})
    return store.get_job(job["id"])


def test_lock_and_strict_export_both_reject_dispatch(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = valid_job(store, tmp_path, codex())
    desk = DeskServer(store)
    with pytest.raises(RuntimeError, match="dispatched"):
        desk.review_save({"job": job["id"], "lock": True, "ai_confirmed": True, "reason": job["review"]["reason"]})
    with pytest.raises(ValueError, match="dispatched"):
        export_tsv(job, tmp_path / "formal.tsv")
    assert not (tmp_path / "formal.tsv").exists() and not store.get_job(job["id"])["review"]["locked_at"]


def test_cannot_waive_builtin_delegation_as_product_functionality(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = valid_job(store, tmp_path, codex())
    with pytest.raises(RuntimeError, match="实际额外委派"):
        DeskServer(store).external_ai_review({"job": job["id"], "side": "A", "user_confirmed": True,
            "trace_sha256": c.audit_trace(job["sides"]["A"]["jsonl_local"])["sha256"], "explanation": "test", "reviewer": "human"})


def test_business_model_api_candidate_requires_hash_bound_manual_explanation(tmp_path):
    rows = codex("exec_command")
    rows[2]["payload"]["arguments"] = json.dumps({"cmd": "curl https://example.invalid/v1/responses"})
    rows[3]["payload"]["output"] = "HTTP 200"
    store = DeskStore(tmp_path / "desk")
    job = valid_job(store, tmp_path, rows)
    assert engines.codex_evidence(job["sides"]["A"]["jsonl_local"])["reason"] is None
    desk = DeskServer(store)
    for name in ("A", "B"):
        report = c.audit_trace(job["sides"][name]["jsonl_local"])
        assert report["status"] == "needs_review"
        desk.external_ai_review({"job": job["id"], "side": name, "user_confirmed": True,
            "trace_sha256": report["sha256"], "explanation": "题面要求实现模型调用客户端；这次调用验证产品入口。", "reviewer": "human"})
    c.require_review_compliance(store.get_job(job["id"]))
    Path(job["sides"]["A"]["jsonl_local"]).write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    assert not c.resolved_report(c.audit_trace(job["sides"]["A"]["jsonl_local"]), store.get_job(job["id"])["sides"]["A"])


def test_annotation_side_conflict_and_unsupported_utf8_premise(tmp_path):
    job = {"prompt": "在 Windows 本地运行。", "review": {"conclusion": "B 更好", "reason": "虽然主要求是utf-8，但综合看还是选择A略好吧。"}}
    issues = c.review_issues(job)
    assert {x["status"] for x in issues} == {"issue", "needs_review"}
    job["review"].update(conclusion="A 更好", reason="B 速度更快，但综合看还是选择A略好吧，Windows兼容性更重要。")
    assert c.review_issues(job) == []


def test_lock_freezes_identity_and_export_rechecks_raw_after_lock(tmp_path):
    rows = [r for r in codex() if not (r.get("type") == "response_item" and r["payload"]["type"].startswith("function_call"))]
    store = DeskStore(tmp_path / "desk")
    job = valid_job(store, tmp_path, rows)
    desk = DeskServer(store)
    desk.review_save({"job": job["id"], "lock": True, "ai_confirmed": True, "reason": job["review"]["reason"]})
    locked = store.get_job(job["id"])
    assert locked["review"]["evidence_binding"]["sides"]["A"]["trace_sha256"]
    with Path(job["sides"]["A"]["jsonl_local"]).open("a", encoding="utf-8") as handle:
        handle.write('{}\n')
    with pytest.raises(ValueError, match="锁定后"):
        c.require_review_compliance(locked)


def test_policy_receipt_never_serializes_authentication(tmp_path):
    (tmp_path / "config.toml").write_text('model = "m"', encoding="utf-8")
    receipt = tmp_path / "receipt.json"
    run_policy.write_receipt(receipt, "codex", {"CODEX_HOME": str(tmp_path), "SECRET": "private-token"}, {"capability_check": "passed"}, ["--model", "m"])
    text = receipt.read_text(encoding="utf-8")
    assert "private-token" not in text and "SECRET" not in text
    assert json.loads(text)["policy_version"] == c.POLICY_VERSION


def test_dimensional_advantage_is_not_a_reversed_final_preference():
    job = {"review": {"conclusion": "A 更好", "reason": "综合看，A更好，B在性能上更优。"}}
    assert c.review_issues(job) == []


@pytest.mark.parametrize("tools,allowed", [(["Bash", "Read", "TaskCreate"], True), (["Agent"], False), (["Workflow"], False), (["SendMessage"], False)])
def test_initialization_rejects_new_or_delegate_tools(tmp_path, tools, allowed):
    import threading
    import io
    from agent_trace_kit.runner import PairRunner
    proc = SimpleNamespace(stdout=[json.dumps({"type": "system", "subtype": "init", "tools": tools})])
    signal = {"agent": "claude"}
    PairRunner._pump_stream(proc, io.StringIO(), tmp_path / "stream.jsonl", threading.Event(), signal)
    assert signal["tools_verified"] == allowed
    assert bool(signal.get("policy_error")) == (not allowed)


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_inherited_provider_frozen_across_sides_and_retries(tmp_path, agent):
    source, home = tmp_path / "source", tmp_path / "home"
    source.mkdir()
    home.mkdir()
    env = {"PATH": "a", "CLAUDE_CONFIG_DIR": str(source), "CODEX_HOME": str(source)}
    if agent == "claude":
        config = source / "settings.json"
        config.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://first.invalid", "ANTHROPIC_AUTH_TOKEN": "fixture-first"}}), encoding="utf-8")
    else:
        config = source / "config.toml"
        config.write_text('model_provider = "vendor"\n[model_providers.vendor]\nbase_url = "https://first.invalid"\nenv_key = "TEST_KEY"\n', encoding="utf-8")
        env["TEST_KEY"] = "fixture-first"
    first, flags = run_policy.frozen_inherit(agent, home, env)
    config.write_text('{}' if agent == "claude" else 'model = "changed"', encoding="utf-8")
    second, next_flags = run_policy.frozen_inherit(agent, home, {**env, "TEST_KEY": "fixture-second", "PATH": "b"})
    assert flags == next_flags and second["PATH"] == "b"
    key = "ANTHROPIC_AUTH_TOKEN" if agent == "claude" else "TEST_KEY"
    assert first[key] == second[key] == "fixture-first"
    import os
    snapshot = home / "inherited-auth-env.json"
    if os.name == "nt":
        assert "fixture-first" not in snapshot.read_text(encoding="utf-8")
    else:
        assert snapshot.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("cmd", ["codex exec 'review'", "codex review --uncommitted", "claude --permission-mode bypassPermissions -p review",
                                 '& "C:\\Program Files\\tools\\claude.exe" -p review',
                                 "python -c 'client.responses.create(model=\"m\")'"])
def test_external_model_invocation_patterns_remain_candidates_not_proof(tmp_path, cmd):
    rows = codex("exec_command")
    rows[2]["payload"]["arguments"] = json.dumps({"cmd": cmd})
    report = c.audit_trace(write(tmp_path / "raw.jsonl", rows))
    assert report["status"] == "needs_review"
    assert report["calls"][0]["kind"] == "external_ai_candidate"


@pytest.mark.parametrize("change", [None, "modified", "personal", "enabled", "changed_list"])
def test_codex_bundled_skills_verified_and_effectively_disabled(tmp_path, monkeypatch, change):
    home = tmp_path / "home"
    home.mkdir()
    calls = []
    def skills(command, env, flags, cwd):
        runtime = Path(env["CODEX_HOME"])
        root = runtime / "skills" / ".system" / "review-agent"
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text("CLI bundled review instructions", encoding="utf-8")
        if runtime == home and change == "modified":
            (root / "SKILL.md").write_text("MUST delegate externally", encoding="utf-8")
        if runtime == home and change == "personal":
            (runtime / "skills" / "personal").mkdir(exist_ok=True)
        disabled = any(flag.startswith("skills.config=") for flag in flags)
        calls.append((runtime, flags))
        return [{"path": str(root / "SKILL.md") + ("-new" if disabled and change == "changed_list" else ""),
                 "enabled": not disabled or change == "enabled"}]
    monkeypatch.setattr(run_policy, "_skills_list", skills)
    if change is None:
        result = run_policy._codex_skills("codex", {"CODEX_HOME": str(home)}, [], tmp_path)
        assert result["skills_check"] == "passed" and result["skills_disabled"] == 1
        assert "enabled=false" in result["extra_args"][1]
        assert calls[0][0] != home and len(calls) == 3
        run_policy.check_home(home)
    else:
        with pytest.raises(ValueError, match="个人指令|不一致|未证明"):
            run_policy._codex_skills("codex", {"CODEX_HOME": str(home)}, [], tmp_path)
        assert (home / "skills" / ".system" / "review-agent" / "SKILL.md").exists()


def test_codex_preflight_requires_bound_home_and_workspace(monkeypatch):
    from agent_trace_kit import procmon
    monkeypatch.setattr(procmon, "run_hidden", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="multi_agent stable false\nmulti_agent_v2 stable false\n"))
    with pytest.raises(ValueError, match="缺隔离目录或工作区"):
        run_policy.preflight("codex", "codex", {}, [])

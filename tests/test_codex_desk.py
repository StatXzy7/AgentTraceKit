"""Synthetic CLI/rollout evidence; no credentials, remote uploads or model calls."""

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from agent_trace_kit import engines, runner, workspace
from agent_trace_kit.checklist import run_checklist
from agent_trace_kit.desk import DeskServer
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.export_tsv import export_tsv, job_row, upload_side
from agent_trace_kit import oss


@pytest.fixture(autouse=True)
def cli_capability_fixture(tmp_path, monkeypatch):
    from agent_trace_kit import run_policy
    monkeypatch.setattr(run_policy, "preflight", lambda *a: {"capability_check": "fixture"})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "user-home"))


def write_rollout(
    home, cwd, sid="session-a", *, complete=True, extra=(), prompt="实现中文模块"
):
    path = (
        home
        / "sessions"
        / "2026"
        / "09"
        / "29"
        / f"rollout-2026-09-29T00-00-00-{sid}.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "session_meta",
            "payload": {"id": sid, "cwd": str(cwd), "cli_version": "test"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "# AGENTS.md instructions\n<environment_context>上下文</environment_context>",
                    }
                ],
            },
        },
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {"type": "turn_context", "payload": {"model": "fixture-model"}},
        {"type": "event_msg", "payload": {"type": "user_message", "message": prompt}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call-1",
                "name": "exec_command",
                "arguments": "{}",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "ok",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "已实现并验证"}],
            },
        },
    ]
    if complete:
        rows.append(
            {
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "turn-1"},
            }
        )
    rows.extend(extra)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_choice_is_persisted_and_legacy_does_not_follow_default(tmp_path):
    store = DeskStore(tmp_path)
    old = store.create_job({"prompt": "旧任务"})
    store.save_settings({"default_agent": "codex", "codex_model": "fixture-model"})
    new = store.create_job({"prompt": "新任务"})
    store.save_settings({"default_agent": "claude", "codex_model": "different"})
    assert new["agent"] == "codex"
    assert new["harness"] == "Codex CLI"
    assert new["codex_model"] == "fixture-model"
    assert engines.job_engine(store.get_job(new["id"])) == "codex"
    old.pop("agent")
    assert engines.job_engine(old) == "claude"
    assert engines.job_engine({}) == "claude"
    assert len(job_row(new)) == 26
    assert job_row(new)[3] == "Codex CLI"


def test_invalid_selection_never_creates_job_or_changes_settings(tmp_path):
    store = DeskStore(tmp_path)
    with pytest.raises(ValueError):
        store.create_job({"prompt": "x", "agent": "other"})
    assert not store.list_jobs()
    with pytest.raises(ValueError):
        store.save_settings({"codex_sandbox": "invalid"})
    assert store.settings()["codex_sandbox"] == "workspace-write"


def test_codex_args_no_prompt_or_claude_flags(monkeypatch):
    monkeypatch.setattr(engines.shutil, "which", lambda value: value)
    args = engines.codex_args({}, {})
    assert args[0:3] == ["codex", "exec", "--json"]
    assert args[-1] == "-"
    assert "workspace-write" in args
    assert 'approval_policy="never"' in args
    assert "--model" not in args and "--ephemeral" not in args
    assert "--permission-mode" not in args
    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert engines.codex_args({}, {"codex_model": "fixture-model"})[-3:] == [
        "--model",
        "fixture-model",
        "-",
    ]


def test_codex_rollout_prompt_model_and_completion(tmp_path):
    path = write_rollout(tmp_path, tmp_path / "work")
    data = engines.codex_evidence(path)
    assert data["users"] == ["实现中文模块"]
    assert data["models"] == ["fixture-model"]
    assert data["session_id"] == "session-a"
    assert data["reason"] is None


@pytest.mark.parametrize(
    "complete,extra,reason",
    [
        (False, [], "missing_completion"),
        (
            True,
            [{"type": "event_msg", "payload": {"type": "turn_aborted"}}],
            "turn_failed",
        ),
        (
            True,
            [
                {
                    "type": "response_item",
                    "payload": {"type": "custom_tool_call", "call_id": "unfinished"},
                }
            ],
            "dangling_tool_result",
        ),
        (
            True,
            [
                {
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "second turn"},
                }
            ],
            "no_assistant",
        ),
    ],
)
def test_codex_cutoff_is_not_complete(tmp_path, complete, extra, reason):
    path = write_rollout(tmp_path, tmp_path / "work", complete=complete, extra=extra)
    assert engines.codex_evidence(path)["reason"] == reason


def test_malformed_tail_cannot_pass(tmp_path):
    path = write_rollout(tmp_path, tmp_path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":')
    assert engines.codex_evidence(path)["reason"] == "malformed_json"


def test_exec_response_only_prompt_with_context_and_crlf(tmp_path):
    """Codex CLI 0.154 exec need not emit event_msg.user_message."""
    prompt = "第一行\n第二行"
    path = write_rollout(tmp_path, tmp_path, prompt=prompt.replace("\n", "\r\n"))
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if row.get("payload", {}).get("type") != "user_message"]
    rows[1]["payload"]["content"][0][
        "text"
    ] = "<recommended_plugins>插件说明</recommended_plugins># AGENTS.md instructions\n<environment_context>环境</environment_context>"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    data = engines.codex_evidence(path, prompt=prompt)
    assert data["users"] == [prompt]
    assert data["reason"] is None
    followup = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "再改一次"}],
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(followup) + "\n")
    data = engines.codex_evidence(path, prompt=prompt)
    assert data["users"] == [prompt, "再改一次"]
    assert data["reason"] == "no_assistant"


def test_complete_with_error_and_rerouted_model_are_preserved(tmp_path):
    path = write_rollout(
        tmp_path,
        tmp_path,
        extra=[
            {
                "type": "event_msg",
                "payload": {
                    "type": "model_reroute",
                    "from_model": "fixture-model",
                    "to_model": "fallback-model",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "error": {"message": "API failed"},
                },
            },
        ],
    )
    data = engines.codex_evidence(path)
    assert data["reason"] == "turn_failed"
    assert data["models"] == ["fallback-model", "fixture-model"]


def test_context_shaped_actual_prompt_is_not_dropped(tmp_path):
    prompt = "<recommended_plugins>实现示例</recommended_plugins>"
    path = write_rollout(tmp_path, tmp_path, prompt=prompt)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows = [row for row in rows if row.get("payload", {}).get("type") != "user_message"]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert engines.codex_evidence(path, prompt=prompt)["users"] == [prompt]


def test_discovery_binds_cwd_id_and_freshness(tmp_path):
    root, work = tmp_path / "codex", tmp_path / "work"
    right = write_rollout(root, work, "right")
    write_rollout(root, tmp_path / "other-work", "wrong-cwd")
    old = write_rollout(root, work, "old")
    os.utime(old, (10, 10))
    env = {"CODEX_HOME": str(root)}
    matches = engines.find_codex_sessions(work, env=env, session_id="right", since=20)
    assert [row["path"] for row in matches] == [str(right)]
    assert not engines.find_codex_sessions(work, env=env, session_id="wrong-cwd")
    assert not engines.find_codex_sessions(work, env=env, session_id="missing")
    assert not engines.find_codex_sessions(work, env=env, session_id="old", since=20)


class FakeJob:
    alive = True
    reason = "fixture"

    def add_pid(self, pid):
        return True

    def terminate(self):
        pass

    def close(self):
        pass


class FakeProc:
    pid = 424242

    def __init__(self, rows, code):
        self.stdout = [json.dumps(row) + "\n" for row in rows]
        self.returncode = code
        self.stdin = io.StringIO()

    def poll(self):
        return self.returncode


@pytest.mark.parametrize(
    "sid,stream_success,complete,exit_code,ok",
    [
        ("session-a", True, True, 0, True),
        ("session-a", False, True, 0, False),
        ("session-a", True, False, 0, False),
        ("wrong-session", True, True, 0, False),
        ("", True, True, 0, False),
        ("session-a", True, True, 1, False),
    ],
)
@pytest.mark.parametrize("policy_diagnostic", [False, True])
def test_codex_attempt_requires_stream_and_matching_rollout(
    tmp_path, monkeypatch, sid, stream_success, complete, exit_code, ok, policy_diagnostic
):
    store = DeskStore(tmp_path / "desk")
    cli_home, work = tmp_path / "codex", tmp_path / "work"
    work.mkdir()
    store.save_settings({"env_overrides": {"CODEX_HOME": str(cli_home)}})
    job = store.create_job(
        {"prompt": '实现中文模块 & echo "不能执行"', "agent": "codex"}
    )
    write_rollout(Path(job["cli_home"]), work, complete=complete, prompt=job["prompt"])
    captured = {}
    rows = [
        {"type": "thread.started", "thread_id": sid},
        # An earlier recoverable notice must not make a later evidence/exit
        # failure eligible for a fresh full run.
        {"type": "error", "message": "unexpected status 504 Gateway Timeout"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "完成"}},
        {"type": "turn.completed" if stream_success else "turn.failed"},
    ]

    def launch(args, **kwargs):
        captured.update(args=args, **kwargs)
        proc = FakeProc(rows, exit_code)
        if policy_diagnostic:
            proc.stdout.insert(1, 'ERROR codex_core::tools::router: exec_command failed: blocked by policy\n')
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner.PairRunner, "_new_kill_job", staticmethod(FakeJob))
    monkeypatch.setattr(runner.procmon, "snapshot", lambda: {})
    monkeypatch.setattr(
        runner, "_feed_stdin", lambda proc, data: captured.update(prompt=data)
    )
    evidence = store.evidence_dir(job["id"])
    result = runner.PairRunner(store)._run_attempt(
        job["id"],
        "A",
        str(work),
        evidence,
        evidence / "a-run.log",
        evidence / "stream.jsonl",
        timeout=60,
        stall_after=0,
        poll_every=5,
        attempt=1,
    )
    assert result["completed"] is ok
    assert result["retryable"] is False
    assert captured["prompt"] == job["prompt"]
    assert job["prompt"] not in captured["args"]
    assert captured["env"]["CODEX_HOME"] == job["cli_home"]
    assert captured["stdin"] == subprocess.PIPE
    assert result["agent"] == "codex"
    assert (
        "turn.completed" in (evidence / "stream.jsonl").read_text(encoding="utf-8")
        if stream_success
        else True
    )


def test_policy_rejection_does_not_kill_running_attempt(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    work, home = tmp_path / "work", tmp_path / "codex"
    work.mkdir()
    job = store.create_job({"prompt": "实现中文模块", "agent": "codex"})
    trace = write_rollout(home, work, prompt=job["prompt"])
    diagnostic = 'ERROR codex_core::tools::router: exec_command failed: blocked by policy\n'
    diagnostic_read, continue_turn = threading.Event(), threading.Event()
    killed = []

    class RecoveringProc(FakeProc):
        def __init__(self):
            super().__init__([], None)
            self.stdout = self.stream()

        def stream(self):
            yield json.dumps({"type": "thread.started", "thread_id": "session-a"}) + "\n"
            yield diagnostic
            diagnostic_read.set()
            assert continue_turn.wait(5), "Runner did not let the turn continue"
            yield json.dumps({"type": "turn.completed"}) + "\n"
            self.returncode = 0

        def poll(self):
            assert diagnostic_read.wait(5)
            return self.returncode

    proc = RecoveringProc()
    ticks = []

    def tick(_):
        ticks.append(1)
        # Keep the CLI alive through a full watchdog iteration after rejection.
        if len(ticks) >= 2:
            continue_turn.set()
            assert pump_done.wait(5)

    pump_done = threading.Event()
    original_pump = runner.PairRunner._pump_stream

    def pump(*args):
        try:
            original_pump(*args)
        finally:
            pump_done.set()

    monkeypatch.setattr(runner.PairRunner, "_pump_stream", staticmethod(pump))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(runner.PairRunner, "_new_kill_job", staticmethod(FakeJob))
    monkeypatch.setattr(runner.procmon, "snapshot", lambda: {})
    monkeypatch.setattr(runner.procmon, "kill_tree", lambda pid: (killed.append(pid), continue_turn.set()))
    monkeypatch.setattr(runner, "_wait_proc", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_feed_stdin", lambda *a: None)
    monkeypatch.setattr(runner.time, "sleep", tick)
    evidence = store.evidence_dir(job["id"])
    result = runner.PairRunner(store)._execute_attempt(
        job["id"], "A", str(work), evidence, evidence / "a-run.log",
        evidence / "stream.jsonl", timeout=60, stall_after=0, poll_every=1,
        attempt=1, settings=store.settings(), session_job=job,
        env={"CODEX_HOME": str(home)}, connection_args=[],
    )
    assert not killed
    assert len(ticks) >= 2
    assert result["completed"]
    assert result["session_id"] == "session-a"
    assert result["session_path"] == str(trace)
    assert diagnostic.strip() in (evidence / "a-run.log").read_text(encoding="utf-8")


def test_codex_checklist_uses_rollout_evidence(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现中文模块", "agent": "codex"})
    work = tmp_path / "work"
    work.mkdir()
    path = write_rollout(tmp_path / "codex", work)
    job["sides"]["A"].update(
        workspace=str(work), session_id="session-a", jsonl_local=str(path)
    )
    items = {item["id"]: item for item in run_checklist(job)["items"]}
    for key in (
        "harness",
        "A_prompt_match",
        "A_single_turn",
        "A_model",
        "A_session_match",
        "A_trace_complete",
    ):
        assert items[key]["ok"], items[key]
    job["sides"]["A"]["session_id"] = "wrong"
    items = {item["id"]: item for item in run_checklist(job)["items"]}
    assert not items["A_session_match"]["ok"]


def test_codex_recollection_keeps_bound_session(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    home, work = tmp_path / "codex", tmp_path / "work"
    work.mkdir()
    store.save_settings({"env_overrides": {"CODEX_HOME": str(home)}})
    job = store.create_job({"prompt": "实现中文模块", "agent": "codex"})
    write_rollout(Path(job["cli_home"]), work, "bound")
    write_rollout(Path(job["cli_home"]), work, "newer-unrelated")
    store.update_side(job["id"], "A", {"workspace": str(work), "session_id": "bound"})
    server = DeskServer(store)
    server._recollect_side(job["id"], "A")
    side = store.get_job(job["id"])["sides"]["A"]
    assert side["session_id"] == "bound"
    assert engines.codex_evidence(side["jsonl_local"])["session_id"] == "bound"


def test_batch_supports_default_and_per_row_engine(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    server = DeskServer(store)
    monkeypatch.setattr(server.runner, "prepare", lambda _: {})
    monkeypatch.setattr(server.runner, "enqueue", lambda _: None)
    result = server.job_batch(
        {
            "agent": "codex",
            "lines": "提示词一\t\t\t\trepo-one\n提示词二\t\t\t\trepo-two\tclaude\n坏项\t\t\t\trepo-three\tinvalid",
        }
    )
    assert len(result["created"]) == 2
    assert len(result["errors"]) == 1
    assert [store.get_job(jid)["harness"] for jid in result["created"]] == [
        "Codex CLI",
        "Claude Code",
    ]


@pytest.mark.parametrize("expected_model,different_b", [("fixture-model", False),
    ("unexpected-requested-model", False), ("", False), ("", True)])
def test_codex_shared_upload_review_lock_and_strict_export(tmp_path, monkeypatch, expected_model, different_b):
    """Synthetic review values exercise plumbing, never a real human attestation."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job(
        {"prompt": "实现中文模块", "agent": "codex", "stack": "Python"}
    )
    sha = "a" * 40
    store.update_job(
        job["id"],
        {
            "baseline_sha": sha,
            "baseline_url": f"https://example.invalid/test/repo/commit/{sha}",
            "baseline_pushed": True,
            "harness_version": "codex-cli test",
            "codex_model": expected_model,
        },
    )
    monkeypatch.setattr(workspace, "is_ancestor", lambda *_: True)
    uploaded = []

    def fake_upload(cfg, path, key):
        uploaded.append(key)
        return {"url": "https://example.invalid/" + key, "size": 10}

    monkeypatch.setattr(oss, "upload_file", fake_upload)
    from types import SimpleNamespace

    for name in ("A", "B"):
        work = tmp_path / name
        work.mkdir()
        trace = write_rollout(tmp_path / "codex", work, "session-" + name)
        if different_b and name == "B":
            trace.write_text(trace.read_text(encoding="utf-8").replace("fixture-model", "other-model"), encoding="utf-8")
        store.update_side(
            job["id"],
            name,
            {
                "workspace": str(work),
                "jsonl_local": str(trace),
                "session_id": "session-" + name,
                "head_sha": sha,
                "initial_sha": sha,
                "head_url": f"https://example.invalid/test/repo/commit/{sha}",
                "pushed": True,
                "status": "done",
                "video_url": f"https://example.invalid/{name}.mp4",
            },
        )
        result = upload_side(
            store, store.get_job(job["id"]), name, SimpleNamespace(key_prefix="fixture")
        )
        store.update_side(job["id"], name, result)
    assert uploaded == ["fixture/session-A.jsonl", "fixture/session-B.jsonl"]
    server = DeskServer(store)
    review = {
        "job": job["id"],
        "lock": True,
        "ai_confirmed": True,
        "validity": "有效",
        "conclusion": "Same",
        "reason": "合成接口测试数据，非真人结论。" * 10,
        "a_delivery_score": "4",
        "a_delivery_description": "合成 A 描述",
        "b_delivery_score": "4",
        "b_delivery_description": "合成 B 描述",
    }
    server.review_save(review)
    assert store.review_locked(store.get_job(job["id"]))
    with pytest.raises(RuntimeError):
        server.job_action({"job": job["id"], "action": "run"})
    if expected_model not in ("", "fixture-model") or different_b:
        with pytest.raises(ValueError, match="使用指定模型|实际模型一致"):
            export_tsv(store.get_job(job["id"]), tmp_path / "pair.tsv", strict=True)
        return
    result = export_tsv(store.get_job(job["id"]), tmp_path / "pair.tsv", strict=True)
    assert result["checklist"]["ready"]
    assert result["tsv"].split("\t")[3] == "Codex CLI"
    assert len(result["tsv"].split("\t")) == 26


def test_stdin_preserves_unicode_metacharacters_and_lf():
    prompt = '中文\n"quoted" & echo injected %PATH%'
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().hex())"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        **runner.procmon.hidden_console_kwargs(),
    )
    runner._feed_stdin(proc, prompt)
    assert proc.stdout.read().strip() == prompt.encode("utf-8").hex()
    assert proc.wait(timeout=5) == 0

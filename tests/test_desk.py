"""Tests for the pair desk: store, workspace/git automation, checklist, TSV, session discovery, OSS signing."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_trace_kit import checklist as cl
from agent_trace_kit import export_tsv
from agent_trace_kit import ghutil
from agent_trace_kit import oss as oss_mod
from agent_trace_kit import workspace as ws
from agent_trace_kit.desk import DeskServer
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.runner import PairRunner


# ---------- git fixtures ----------

def _git(args, cwd):
    import subprocess
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def baseline_repo(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = tmp_path / "base"
    base.mkdir()
    _git(["init", "-b", "main"], base)
    _git(["config", "user.email", "t@example.com"], base)
    _git(["config", "user.name", "T"], base)
    (base / ".gitignore").write_text(".env\n", encoding="utf-8")
    (base / "README.md").write_text("# baseline\n", encoding="utf-8")
    _git(["add", "-A"], base)
    _git(["commit", "-m", "baseline"], base)
    _git(["remote", "add", "origin", str(bare)], base)
    _git(["push", "-u", "origin", "main"], base)
    return base


def test_store_create_update_and_crash_recovery(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "do X", "baseline_repo": str(tmp_path)})
    assert job["status"] == "draft"
    assert job["sides"]["A"]["branch"].endswith("-a")
    store.update_side(job["id"], "A", {"status": "running"})
    # simulate a fresh process after crash
    PairRunner(store).recover()
    recovered = store.get_job(job["id"])
    assert recovered["sides"]["A"]["status"] == "failed"
    assert "重跑" in recovered["sides"]["A"]["error"]
    assert len(store.list_jobs()) == 1


def test_prepare_workspaces_ancestry_and_push(baseline_repo, tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "baseline_repo": str(baseline_repo)})
    snap = ws.snapshot_baseline(baseline_repo)
    assert ws.is_sha40(snap["sha"])

    pair_dir = tmp_path / "ws"
    info_a = ws.prepare_side_workspace(baseline_repo, pair_dir / "a", "pair-x-a")
    info_b = ws.prepare_side_workspace(baseline_repo, pair_dir / "b", "pair-x-b")
    assert info_a["head"] == snap["sha"] == info_b["head"]  # same start point

    # simulate the model producing changes on side A
    (Path(info_a["workspace"]) / "engine.py").write_text("print('A')\n", encoding="utf-8")
    fin = ws.finalize_side(info_a["workspace"], "pair-x-a", "A product")
    assert ws.is_sha40(fin["sha"])
    assert ws.is_ancestor(info_a["workspace"], snap["sha"], fin["sha"])
    assert ws.sha_pushed(info_a["workspace"], fin["sha"], "pair-x-a")

    # permalink shape for a real https remote
    _git(["config", "remote.origin.url", "https://github.com/org/repo.git"], info_a["workspace"])
    url = ws.commit_permalink(info_a["workspace"], fin["sha"])
    assert url == f"https://github.com/org/repo/commit/{fin['sha']}"


def _make_session_file(projects_dir: Path, encoded_dir: str, session_id: str, cwd: str, prompt: str):
    d = projects_dir / encoded_dir
    d.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "mode", "sessionId": session_id, "cwd": None},
        {"type": "user", "sessionId": session_id, "cwd": cwd,
         "message": {"role": "user", "content": [{"type": "text", "text": prompt}]}},
        {"type": "assistant", "sessionId": session_id, "cwd": cwd, "message": {"content": "done"}},
    ]
    p = d / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")
    return p


def test_find_session_jsonl_by_cwd(tmp_path, monkeypatch):
    home = tmp_path / "claudehome"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    wdir = tmp_path / "ws" / "a"
    wdir.mkdir(parents=True)
    p = _make_session_file(home / "projects", ws._encode_cwd(wdir),
                           "sid-aaa", str(wdir), "build an engine")
    found = ws.find_session_jsonl(wdir)
    assert len(found) == 1 and found[0]["session_id"] == "sid-aaa"
    # a session from another directory must not match
    other = tmp_path / "ws" / "b"
    other.mkdir()
    assert ws.find_session_jsonl(other) == []
    assert p.exists()


def test_clean_path_strips_wrapping_quotes():
    from agent_trace_kit.desk_store import clean_path
    assert clean_path(r'"D:\myprojects\repo"') == r"D:\myprojects\repo"
    assert clean_path(r"'D:\myprojects\repo'") == r"D:\myprojects\repo"
    assert clean_path("  D:\\x  ") == "D:\\x"
    assert clean_path("") == ""


def _write_transcript(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records),
        encoding="utf-8",
    )
    return path


def _tool_tail_records(prompt: str, final_text: str | None, *, final_is_api_error: bool = False):
    """user prompt -> assistant tool_use -> tool_result -> optional final assistant."""
    recs = [
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": prompt}]}},
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Write", "input": {}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}},
    ]
    if final_text is not None:
        if final_is_api_error:
            recs.append({"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 504,
                         "message": {"role": "assistant", "model": "<synthetic>",
                                     "content": [{"type": "text", "text": final_text}]}})
        else:
            recs.append({"type": "assistant", "message": {"role": "assistant",
                                                           "content": [{"type": "text", "text": final_text}]}})
    return recs


def _api_error_record(text: str = "API Error: 504 Gateway Time-out") -> dict:
    return {"type": "assistant", "isApiErrorMessage": True, "apiErrorStatus": 504,
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": text}]}}


def test_transcript_interruption_classification(tmp_path):
    p = tmp_path / "cut-504.jsonl"
    _write_transcript(p, _tool_tail_records(
        "build it", "API Error: 504 Gateway Time-out", final_is_api_error=True))
    assert ws.transcript_interruption_reason(p) == "api_error"
    assert not ws.session_turn_complete(p)
    assert ws.session_has_assistant(p)

    p2 = tmp_path / "cut-stall.jsonl"
    _write_transcript(p2, _tool_tail_records("build it", None))
    assert ws.transcript_interruption_reason(p2) == "dangling_tool_result"
    assert not ws.session_turn_complete(p2)

    p3 = tmp_path / "complete.jsonl"
    _write_transcript(p3, _tool_tail_records("build it", "全部完成，测试已通过"))
    assert ws.transcript_interruption_reason(p3) is None
    assert ws.session_turn_complete(p3) and ws.session_has_assistant(p3)

    p4 = tmp_path / "empty.jsonl"
    _write_transcript(p4, [{"type": "ai-title", "aiTitle": "x"}])
    assert ws.transcript_interruption_reason(p4) == "no_assistant"
    assert not ws.session_has_assistant(p4)

    # A real model answer that merely quotes the words "API Error" is NOT a cut.
    p5 = tmp_path / "quoted-error.jsonl"
    _write_transcript(p5, _tool_tail_records("document 504s", "API Error 处理说明：收到 504 时应重试。"))
    assert ws.transcript_interruption_reason(p5) is None

    # Trailing synthetic 504 AFTER a normal closing answer = complete turn
    # (CLI exits non-zero on a post-turn auxiliary call, but the product is done).
    recs = _tool_tail_records("build it", "全部完成，测试已通过")
    recs.append(_api_error_record())
    p6 = tmp_path / "trailing-504.jsonl"
    _write_transcript(p6, recs)
    assert ws.transcript_interruption_reason(p6) is None


class _FakeProc:
    """Minimal subprocess.Popen double: fixed stdout lines, already exited."""

    def __init__(self, lines: list[str], returncode: int):
        self.stdout = list(lines)
        self.pid = 424242
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass


def _run_one_attempt(store, tmp_path, transcript_path: Path,
                     stream_lines: list[str], returncode: int, monkeypatch):
    from agent_trace_kit import runner as rn
    if not store.list_jobs():
        store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    fake_proc = _FakeProc(stream_lines, returncode)
    monkeypatch.setattr(rn.subprocess, "Popen", lambda *a, **k: fake_proc)
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w: [{"path": str(transcript_path), "session_id": "sid-x",
                                    "mtime": "9999999999"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w: 0.0))
    evidence = store.evidence_dir(job_id)
    return rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        timeout=60, stall_after=30, poll_every=5, attempt=1,
    )


def _result_line(is_error: bool) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                       "num_turns": 3, "total_cost_usd": 0.0,
                       "result": "API Error: 429 rate limited" if is_error else "完成"}) + "\n"


def test_runner_passes_permission_mode(tmp_path, monkeypatch):
    """Default is bypassPermissions (acceptEdits auto-denies all Bash under -p);
    a settings override flows through to the CLI argv."""
    from agent_trace_kit import runner as rn
    store = DeskStore(tmp_path / "desk")
    store.create_job({"prompt": "实现一个模块"})
    job_id = store.list_jobs()[0]["id"]
    wdir = tmp_path / "w"
    wdir.mkdir(exist_ok=True)
    captured = {}

    class CapturingPopen(_FakeProc):
        def __init__(self, args, **kw):
            captured["args"] = args
            super().__init__([_result_line(False)], 0)

    def fake_popen(args, **kw):
        return CapturingPopen(args, **kw)

    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    monkeypatch.setattr(rn.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ws, "find_session_jsonl",
                        lambda w: [{"path": str(complete), "session_id": "sid-x", "mtime": "9"}])
    monkeypatch.setattr(rn.PairRunner, "_latest_transcript_mtime", staticmethod(lambda w: 0.0))
    evidence = store.evidence_dir(job_id)
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert captured["args"][captured["args"].index("--permission-mode") + 1] == "bypassPermissions"

    store.save_settings({"permission_mode": "plan"})
    rn.PairRunner(store)._run_attempt(
        job_id, "A", str(wdir), evidence, evidence / "a-run.log",
        timeout=60, stall_after=30, poll_every=5, attempt=1)
    assert captured["args"][captured["args"].index("--permission-mode") + 1] == "plan"


def test_runner_accepts_exit1_trailing504_after_clean_turn(tmp_path, monkeypatch):
    """exit 1 + 504 stream line + clean result + complete transcript = success."""
    store = DeskStore(tmp_path / "desk")
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成，测试通过"))
    out = _run_one_attempt(store, tmp_path, complete,
                           ["API Error: 504 trailing auxiliary failure\n", _result_line(False)], 1,
                           monkeypatch)
    assert out["completed"] is True and out["code"] == 1


def test_runner_rejects_result_is_error(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    complete = _write_transcript(
        tmp_path / "complete.jsonl", _tool_tail_records("实现一个模块", "完成"))
    out = _run_one_attempt(store, tmp_path, complete, [_result_line(True)], 1, monkeypatch)
    assert out["completed"] is False
    assert "result 错误" in out["failure"]


def test_runner_rejects_midturn_cut_even_if_stream_says_success(tmp_path, monkeypatch):
    """f02ec37c: stream says success but transcript tail is a terminal 504."""
    store = DeskStore(tmp_path / "desk")
    cut = _write_transcript(
        tmp_path / "cut.jsonl",
        _tool_tail_records("实现一个模块", "API Error: 504 Gateway Time-out",
                           final_is_api_error=True))
    out = _run_one_attempt(store, tmp_path, cut, [_result_line(False)], 0, monkeypatch)
    assert out["completed"] is False
    assert "截断" in out["failure"]


def test_retry_backoff_grows_and_caps():
    import random as _random
    from agent_trace_kit.runner import retry_backoff_seconds
    rng = _random.Random(0)
    waits = [retry_backoff_seconds(a, rng) for a in range(2, 8)]
    assert waits[0] > 0
    assert waits[0] < waits[1] < waits[2]
    assert all(w <= 120 for w in waits)
    assert waits[-1] <= 120


def test_checklist_blocks_cut_transcript(tmp_path, baseline_repo, monkeypatch):
    """A done side whose transcript tail is a gateway 504 must block export."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现一个模块", "stack": "Python",
                            "baseline_repo": str(baseline_repo)})
    ws.snapshot_baseline(baseline_repo)
    info = ws.prepare_side_workspace(baseline_repo, tmp_path / "ws" / "a", "pair-x-a")
    fin = ws.finalize_side(info["workspace"], "pair-x-a", "A product")
    cut_jsonl = _write_transcript(
        tmp_path / "ev" / "a-cut.jsonl",
        _tool_tail_records(job["prompt"], "API Error: 504 Gateway Time-out",
                           final_is_api_error=True))
    store.update_side(job["id"], "A", {
        "workspace": info["workspace"], "session_id": "sid-cut",
        "jsonl_local": str(cut_jsonl), "head_sha": fin["sha"],
        "head_url": f"https://github.com/org/repo/commit/{fin['sha']}",
        "pushed": True, "status": "done",
        "trace_url": "https://oss.example.com/sid-cut.jsonl",
    })
    job = store.get_job(job["id"])
    blocked = {x["id"]: x for x in cl.run_checklist(job)["items"]
               if x["blocking"] and not x["ok"]}
    assert "A_trace_complete" in blocked
    # declaring the pair an engineering fault downgrades it to non-blocking audit
    store.update_review(job["id"], {"validity": "作废-工程故障"})
    job = store.get_job(job["id"])
    a_item = next(x for x in cl.run_checklist(job)["items"] if x["id"] == "A_trace_complete")
    assert not a_item["ok"] and not a_item["blocking"]



def test_checklist_blocks_then_passes(tmp_path, baseline_repo, monkeypatch):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({
        "prompt": "实现一个流处理引擎",
        "stack": "Go",
        "baseline_repo": str(baseline_repo),
    })
    report = cl.run_checklist(job)
    assert not report["ready"]
    assert report["blocking_count"] > 0

    # fill the job the way the runner would after two successful sides
    snap = ws.snapshot_baseline(baseline_repo)
    pair_dir = tmp_path / "ws"
    home = tmp_path / "claudehome"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    prompt = job["prompt"]
    sides = {}
    for name in ("A", "B"):
        info = ws.prepare_side_workspace(baseline_repo, pair_dir / name.lower(), f"pair-x-{name.lower()}")
        (Path(info["workspace"]) / f"{name.lower()}.py").write_text("print(1)\n", encoding="utf-8")
        fin = ws.finalize_side(info["workspace"], info["branch"], f"{name} product")
        sid = f"sid-{name.lower()}-111"
        jsonl = _make_session_file(home / "projects", ws._encode_cwd(info["workspace"]),
                                   sid, info["workspace"], prompt + " 请严格测试")
        # prompt is contained in the user message (allowance: checklist uses substring)
        sides[name] = {**info, "fin": fin, "jsonl": jsonl, "sid": sid}

    # rewrite the user message to equal the exact prompt for the strict match
    for name in ("A", "B"):
        recs = [json.loads(l) for l in sides[name]["jsonl"].read_text(encoding="utf-8").splitlines()]
        recs[1]["message"]["content"][0]["text"] = prompt
        sides[name]["jsonl"].write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs), encoding="utf-8")

    for name in ("A", "B"):
        store.update_side(job["id"], name, {
            "workspace": sides[name]["workspace"], "session_id": sides[name]["sid"],
            "jsonl_local": str(sides[name]["jsonl"]),
            "head_sha": sides[name]["fin"]["sha"], "head_url":
            f"https://github.com/org/repo/commit/{sides[name]['fin']['sha']}",
            "pushed": True, "status": "done",
            "trace_url": f"https://oss.example.com/{sides[name]['sid']}.jsonl",
            "video_local": str(tmp_path / f"{name}.mp4"),
        })
        (tmp_path / f"{name}.mp4").write_bytes(b"FAKEMP4")
    job = store.update_job(job["id"], {
        "baseline_sha": snap["sha"],
        "baseline_url": f"https://github.com/org/repo/commit/{snap['sha']}",
        "baseline_pushed": True, "harness_version": "2.1.260",
    })
    job = store.update_review(job["id"], {"validity": "有效", "conclusion": "A 更好", "reason": "A 恢复路径无死锁，" * 5, "reviewer": "Vincent"})
    report = cl.run_checklist(job)
    blockers = [x["id"] for x in report["items"] if x["blocking"] and not x["ok"]]
    assert blockers == [], blockers
    assert report["ready"]


def test_export_tsv_headers_and_strict_gate(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p"})
    with pytest.raises(ValueError):
        export_tsv.export_tsv(job, tmp_path / "x.tsv", strict=True)
    # header row always has the 23 spec columns, in submission-template order
    assert len(export_tsv.HEADERS) == 23
    assert export_tsv.HEADERS[0] == "User Prompt"
    assert export_tsv.HEADERS[1] == "提交人"
    assert export_tsv.HEADERS[17] == "有效性"
    assert export_tsv.HEADERS[20:22] == ["内部质检", "质检反馈"]
    assert export_tsv.HEADERS[-1] == "备注"


def test_checklist_voided_pair_skips_gsb_and_run_blockers(tmp_path):
    """作废-工程故障: a failed side without a GSB judgement still exports (audit record)."""
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "p", "stack": "Go"})
    store.update_side(job["id"], "A", {"status": "failed", "error": "claude crashed 中断"})
    store.update_side(job["id"], "B", {"status": "failed", "error": "claude crashed 中断"})
    job = store.update_review(job["id"], {
        "validity": "作废-工程故障", "reviewer": "徐子扬",
    })
    report = cl.run_checklist(job)
    blocking_ids = {x["id"] for x in report["items"] if x["blocking"] and not x["ok"]}
    assert "conclusion" not in blocking_ids
    assert "reason" not in blocking_ids
    assert "A_run" not in blocking_ids and "B_run" not in blocking_ids
    # validity itself must be selected, and an unselected one still blocks
    assert "validity" not in blocking_ids
    job2 = store.create_job({"prompt": "p2", "stack": "Go"})
    assert any(x["id"] == "validity" and x["blocking"] and not x["ok"]
               for x in cl.run_checklist(job2)["items"])


def test_oss_sigv4_request_and_public_url():
    received = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            received["body"] = self.rfile.read(length)
            received["auth"] = self.headers.get("Authorization", "")
            received["amz_date"] = self.headers.get("X-Amz-Date", "")
            self.send_response(200); self.send_header("ETag", '"abc"'); self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), H)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True); t.start()
    try:
        cfg = oss_mod.OssConfig(
            endpoint=f"http://127.0.0.1:{port}", bucket="bk",
            access_key_id="JDC_TEST", secret_access_key="secret", region="cn-north-1",
        )
        f = Path(__file__).parent / "_tmp_oss.bin"
        f.write_bytes(b"hello-oss")
        try:
            info = oss_mod.upload_file(cfg, f, "pairwise/sid.jsonl")
        finally:
            f.unlink(missing_ok=True)
        assert received["body"] == b"hello-oss"
        assert received["auth"].startswith("AWS4-HMAC-SHA256 Credential=JDC_TEST/")
        assert "/cn-north-1/s3/aws4_request, " in received["auth"]
        assert info["url"].endswith("/bk/pairwise/sid.jsonl")
        assert info["size"] == 9
    finally:
        server.shutdown()


def test_oss_config_from_secrets_file(tmp_path):
    secrets = tmp_path / "secrets.env"
    secrets.write_text(
        "# comment\nOSS_ACCESS_KEY_ID=\"JDC_X\"\nOSS_SECRET_ACCESS_KEY=sec\n", encoding="utf-8")
    mapping = {"oss_endpoint": "https://s3.cn-north-1.jdcloud-oss.com", "oss_bucket": "b1"}
    cfg = oss_mod.config_from_mapping(mapping, secrets)
    assert cfg is not None and cfg.access_key_id == "JDC_X" and cfg.bucket == "b1"
    assert oss_mod.config_from_mapping({"oss_bucket": ""}, secrets) is None


# ---------- one-click baseline provisioning (ghutil + workspace + runner) ----------

def test_repo_name_validation():
    from agent_trace_kit import ghutil
    assert ghutil.validate_repo_name("rate-limiter_lib") == ""
    assert ghutil.validate_repo_name("a") == ""
    assert ghutil.validate_repo_name("") != ""
    assert ghutil.validate_repo_name("bad name") != ""       # space
    assert ghutil.validate_repo_name("bad/name") != ""
    assert ghutil.validate_repo_name("con") != ""            # Windows reserved
    assert ghutil.validate_repo_name("con.txt") != ""        # reserved base + extension
    assert ghutil.validate_repo_name("nul.md") != ""
    assert ghutil.validate_repo_name("-leaddash") != ""      # option-injection guard
    assert ghutil.validate_repo_name("a..b") != ""
    assert ghutil.validate_repo_name("trailing.") != ""
    assert ghutil.validate_repo_name("x" * 101) != ""


def test_render_readme_default_blurb_contains_title():
    from agent_trace_kit import ghutil
    out = ghutil.render_readme("my-task", "")
    assert out.startswith("# my-task\n") and "基线" in out
    assert "一句话说明" in ghutil.render_readme("r", "一句话说明")


class _FakeGh:
    """Scripted gh stand-in: records create calls, returns None or a repo on view."""

    def __init__(self, existing: "ghutil.RemoteRepo | None" = None, login: str = "StatXzy7",
                 url: str = ""):
        self._existing = existing
        self._login = login
        self._url = url
        self.created: list[tuple[str, str, bool]] = []

    def viewer_login(self) -> str:
        return self._login

    def repo_view(self, owner: str, name: str):
        return self._existing

    def repo_create(self, owner: str, name: str, description: str, private: bool):
        self.created.append((name, description, private))
        repo = ghutil.RemoteRepo(
            name=name, owner=owner,
            url=self._url or f"https://github.com/{owner}/{name}",
            default_branch="main", visibility="PRIVATE" if private else "PUBLIC",
        )
        self._existing = repo
        return repo


def test_ensure_remote_repo_creates_when_missing_and_reuses_when_present():
    from agent_trace_kit import ghutil
    fake = _FakeGh(existing=None)
    repo, created = ghutil.ensure_remote_repo("new-task", "blurb", private=False, gh=fake)
    assert created is True and repo.full_name == "StatXzy7/new-task"
    assert repo.visibility == "PUBLIC" and repo.default_branch == "main"
    assert fake.created == [("new-task", "blurb", False)]
    # second call finds the repo and never recreates it
    repo2, created2 = ghutil.ensure_remote_repo("new-task", "blurb", private=True, gh=fake)
    assert created2 is False and repo2.url == repo.url and len(fake.created) == 1


def test_provision_baseline_folder_seeds_empty_remote_and_is_idempotent(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"

    info = ws.provision_baseline_folder(parent, "task-x", bare.as_uri(), "main", "题目说明")
    target = Path(info["path"])
    assert ws.is_sha40(info["sha"]) and info["branch"] == "main" and info["seeded"] is True
    assert (target / "README.md").read_text(encoding="utf-8").startswith("# task-x\n\n题目说明")
    assert (target / ".gitignore").exists()
    # the initial commit is reachable on the bare remote's main
    assert ws._remote_branch_exists(target, "main")

    # re-running is a no-op: no new seed commit, same sha
    again = ws.provision_baseline_folder(parent, "task-x", bare.as_uri(), "main", "题目说明")
    assert again["seeded"] is False and again["sha"] == info["sha"]


def test_provision_baseline_folder_clones_existing_remote_without_injecting(tmp_path):
    """A remote that already has history is cloned as-is (README blurb not injected)."""
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    _git(["config", "user.email", "t@example.com"], seed)
    _git(["config", "user.name", "T"], seed)
    (seed / "problem.md").write_text("题目本体\n", encoding="utf-8")
    _git(["add", "-A"], seed)
    _git(["commit", "-m", "题目初始代码"], seed)
    _git(["remote", "add", "origin", str(bare)], seed)
    _git(["push", "-u", "origin", "main"], seed)
    original_sha = ws.head_sha(seed)

    parent = tmp_path / "github-base"
    info = ws.provision_baseline_folder(parent, "task-y", bare.as_uri(), "main", "忽略此说明")
    assert info["seeded"] is False and info["sha"] == original_sha
    target = Path(info["path"])
    assert (target / "problem.md").exists()
    assert not (target / "README.md").exists()  # existing history is never mutated


def test_provision_rejects_unrelated_non_git_folder(tmp_path):
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"
    occupied = parent / "task-z"
    occupied.mkdir(parents=True)
    (occupied / "notes.txt").write_text("user file", encoding="utf-8")
    with pytest.raises(ws.GitError):
        ws.provision_baseline_folder(parent, "task-z", bare.as_uri(), "main", "")


def test_job_carries_provisioning_fields_from_settings(tmp_path):
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"baseline_parent_dir": str(tmp_path / "base"),
                         "github_owner": "", "github_private": True})
    job = store.create_job({"prompt": "p", "github_repo": "new-repo", "github_readme": "说明"})
    assert job["github_repo"] == "new-repo"
    assert job["github_readme"] == "说明"
    assert job["github_private"] is True
    assert job["baseline_parent_dir"] == str(tmp_path / "base")
    assert job["baseline_repo"] == ""


def test_path_vs_repo_name_heuristic():
    f = DeskServer._looks_like_local_path
    assert f(r"D:\myprojects\GoletaLab数据标注\github-base\x")
    assert f("/home/u/repos/x")
    assert not f("rate-limiter-lib")
    assert not f("my_repo.name-1")
    assert f("C:/work/x")
    assert not f("")


def test_prepare_end_to_end_provisions_github_repo(tmp_path, monkeypatch):
    """prepare() on a name-only job: fake gh + local bare remote -> seeded baseline + A/B."""
    from agent_trace_kit import runner as rn
    import subprocess

    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"
    store = DeskStore(tmp_path / "desk")
    store.save_settings({"baseline_parent_dir": str(parent)})
    job = store.create_job({"prompt": "实现一个模块", "github_repo": "e2e-task",
                            "github_readme": "端到端题目"})

    fake = _FakeGh(existing=None, url=bare.as_uri())
    _orig_ensure = ghutil.ensure_remote_repo

    def _fake_ensure(name, desc, *, private, owner="", gh=None):
        return _orig_ensure(name, desc, private=private, owner=owner, gh=fake)

    monkeypatch.setattr(rn.ghutil, "ensure_remote_repo", _fake_ensure)
    report = rn.PairRunner(store).prepare(job["id"])
    assert report["provisioned"] is not None
    assert report["provisioned"]["created"] is True
    assert report["provisioned"]["seeded"] is True
    assert fake.created[0][0] == "e2e-task"

    done = store.get_job(job["id"])
    assert done["baseline_repo"] == str(parent / "e2e-task")
    assert ws.is_sha40(done["baseline_sha"]) and done["status"] == "ready"
    # both isolated side workspaces were copied from the seeded baseline
    for side_name in ("A", "B"):
        wdir = done["sides"][side_name]["workspace"]
        assert Path(wdir).is_dir() and (Path(wdir) / "README.md").exists()
    # a second prepare reuses the local repo (no new remote create, same baseline sha)
    rn.PairRunner(store).prepare(job["id"])
    assert len(fake.created) == 1


def test_provision_refuses_zero_commit_folder_with_user_files(tmp_path):
    """HIGH: a git-init'd folder holding untracked files must never have them
    swept into a (public by default) initial commit/push."""
    import subprocess
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(bare)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    parent = tmp_path / "github-base"
    target = parent / "task-w"
    target.mkdir(parents=True)
    _git(["init", "-b", "main"], target)
    (target / "secret.txt").write_text("TOP SECRET\n", encoding="utf-8")  # untracked
    with pytest.raises(ws.GitError, match="拒绝自动提交"):
        ws.provision_baseline_folder(parent, "task-w", bare.as_uri(), "main", "")
    # nothing was pushed to the bare remote
    out, _, _ = subprocess_run_capture(["git", "--git-dir", str(bare), "ls-remote", "--heads"])
    assert out.strip() == ""


def subprocess_run_capture(args):
    import subprocess
    p = subprocess.run(args, text=True, capture_output=True)
    return p.stdout, p.stderr, p.returncode


def test_canonical_remote_matches_https_and_ssh_forms():
    f = ws.canonical_remote
    assert f("https://github.com/StatXzy7/task-x.git") == "github.com/statxzy7/task-x"
    assert f("git@github.com:StatXzy7/task-x.git") == "github.com/statxzy7/task-x"
    assert f("https://github.com/StatXzy7/task-y") != f("https://github.com/StatXzy7/task-x")
    assert f("not-a-remote") == ""


def test_ensure_remote_repo_rejects_owner_other_than_login():
    gh = _FakeGh(existing=None)
    with pytest.raises(ghutil.GitHubError, match="不一致"):
        ghutil.ensure_remote_repo("x", "d", private=False, gh=gh, owner="someone-else")
    assert gh.created == []  # nothing created


def test_desk_csrf_and_host_defenses(tmp_path):
    """C1: only same-loopback application/json POSTs reach the action routes."""
    import http.client
    import threading
    from agent_trace_kit.desk import _ExclusiveServer, DeskServer

    desk = DeskServer(DeskStore(tmp_path / "desk"))
    httpd = _ExclusiveServer(("127.0.0.1", 0), desk.make_handler())
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        def post(headers, body=b"{}"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("POST", "/api/jobs", body=body, headers=headers)
            r = conn.getresponse()
            r.read()
            return r.status

        # same-origin JSON request is accepted and routed
        assert post({"Content-Type": "application/json"}) == 200
        # cross-site "simple" request content type is rejected (forces preflight)
        assert post({"Content-Type": "text/plain"}) == 415
        # explicit cross-origin browser request blocked
        assert post({"Content-Type": "application/json", "Origin": "http://evil.example"}) == 403
        # DNS-rebinding style Host header blocked
        assert post({"Content-Type": "application/json", "Host": "evil.example"}) == 403
        # loopback origin but a different port blocked
        assert post({"Content-Type": "application/json", "Origin": "http://127.0.0.1:9999"}) == 403
    finally:
        httpd.shutdown()
        httpd.server_close()

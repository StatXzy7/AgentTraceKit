"""Tests for the pair desk: store, workspace/git automation, checklist, TSV, session discovery, OSS signing."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from agent_trace_kit import checklist as cl
from agent_trace_kit import export_tsv
from agent_trace_kit import oss as oss_mod
from agent_trace_kit import workspace as ws
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

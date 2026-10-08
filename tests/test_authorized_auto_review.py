"""Authorized AI drafts never choose validity or assert human-only review."""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess

import pytest

from agent_trace_kit import desk as desk_mod
from agent_trace_kit.demo import artifact_key
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.export_tsv import HEADERS, job_row


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def ready_pair(tmp_path):
    store = DeskStore(tmp_path / "desk")
    job = store.create_job({"prompt": "实现并测试算法。", "agent": "codex", "cli_model": "auto_model/urm"})
    desk = desk_mod.DeskServer(store)
    bindings = {}
    for name in ("A", "B"):
        job = store.update_side(job["id"], name, {
            "status": "done", "head_sha": name.lower() * 40, "session_id": f"session-{name}",
            "finished_at": "2026-09-30T10:00:00+00:00",
        })
        side = job["sides"][name]
        report = {"status": "ready", "human_reviewed": False, "run_id": f"demo-{name}",
                  "artifact_key": artifact_key(side), "side": name, "job": job["id"]}
        folder = store.evidence_dir(job["id"]) / "demo" / name.lower() / report["run_id"]
        folder.mkdir(parents=True)
        report_path = folder / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        video = folder / "video.mp4"
        video.write_bytes(b"fake-video-for-hash-validation")
        trace = store.evidence_dir(job["id"]) / f"{name.lower()}-session.jsonl"
        trace.write_text(json.dumps({"session_id": side["session_id"]}) + "\n", encoding="utf-8")
        store.update_side(job["id"], name, {"demo": report, "video_local": str(video), "jsonl_local": str(trace)})
        bindings[name] = {"head_sha": side["head_sha"], "session_id": side["session_id"],
                          "demo_artifact_key": report["artifact_key"], "demo_run_id": report["run_id"],
                          "demo_report_sha256": digest(report_path), "video_sha256": digest(video),
                          "trace_url": "", "video_url": "", "jsonl_sha256": digest(trace)}
    evidence = store.evidence_dir(job["id"]) / "auto-review" / "commands.json"
    evidence.parent.mkdir()
    evidence.write_text(json.dumps({"human_reviewed": False, "commands": [
        {"side": "A", "argv": ["python", "-m", "unittest"], "exit_code": 0},
        {"side": "B", "argv": ["python", "-m", "unittest"], "exit_code": 0},
    ]}, ensure_ascii=False), encoding="utf-8")
    body = {"job": job["id"], "origin": "ai-authorized", "model": "auto_model/urm",
            "conclusion": "Same", "reason": "自动执行记录表明两侧核心算法和边界样例均通过，尚未完成更大规模误差扫描；当前证据没有显示明显质量差异。" * 2,
            "a_delivery_score": 4, "a_delivery_description": "核心算法及边界样例通过，较大规模误差尚未验证。",
            "b_delivery_score": "4", "b_delivery_description": "核心算法及边界样例通过，较大规模误差尚未验证。",
            "qc_status": "自动检查完成，待用户判断有效性", "qc_feedback": "两侧执行与录制证据已绑定；未作人工实测声明。",
            "note": "评价依据自动命令日志；有效性留空。", "evidence_bindings": bindings,
            "evidence_files": [{"path": "auto-review/commands.json", "sha256": digest(evidence)}]}
    return store, desk, body


def test_auto_review_preserves_user_only_fields_and_provenance(ready_pair):
    store, desk, body = ready_pair
    saved = desk.auto_review_save(body)
    review = saved["review"]
    assert review["validity"] == ""
    assert review["ai_confirmed"] is False
    assert review["ai_confirmed_at"] == review["locked_at"] == ""
    assert review["auto_review"]["human_reviewed"] is False
    assert review["auto_review"]["evidence_bindings"] == body["evidence_bindings"]
    assert review["a_delivery_score"] == review["b_delivery_score"] == "4"
    assert "授权 AI 辅助评价" in review["note"]
    source = copy.deepcopy(review["auto_review"])
    desk.review_save({"job": saved["id"], "validity": "有效"})
    later = store.get_job(saved["id"])["review"]
    assert later["validity"] == "有效" and later["auto_review"] == source
    assert later["ai_confirmed"] is False and not later["locked_at"]
    row = job_row(store.get_job(saved["id"]))
    assert len(row) == len(HEADERS) == 26
    assert row[23:25] == [body["qc_status"], body["qc_feedback"]]
    assert row[25] == later["note"]
    desk.review_save({"job": saved["id"], "note": "用户补充说明。"})
    assert "授权 AI 辅助评价" in job_row(store.get_job(saved["id"]))[25]


@pytest.mark.parametrize("field,value", [("validity", "有效"), ("ai_confirmed", False),
                                         ("locked_at", ""), ("lock", False)])
def test_auto_route_refuses_any_human_gate_field(ready_pair, field, value):
    store, desk, body = ready_pair
    body[field] = value
    with pytest.raises(RuntimeError, match="不能设置"):
        desk.auto_review_save(body)
    assert not store.get_job(body["job"])["review"]["conclusion"]


@pytest.mark.parametrize("field,value", [("a_delivery_score", True), ("b_delivery_score", 6),
                                         ("a_delivery_description", ""), ("conclusion", "A wins"),
                                         ("reason", "太短"), ("model", "another-model")])
def test_auto_route_rejects_incomplete_or_inconsistent_fields(ready_pair, field, value):
    _, desk, body = ready_pair
    body[field] = value
    with pytest.raises(RuntimeError):
        desk.auto_review_save(body)


@pytest.mark.parametrize("kind", ["head", "session", "live", "demo_pending", "report", "video", "commands"])
def test_auto_route_rejects_changed_or_unfinished_evidence(ready_pair, monkeypatch, kind):
    store, desk, body = ready_pair
    job = store.get_job(body["job"])
    if kind == "head":
        store.update_side(job["id"], "A", {"head_sha": "c" * 40})
    elif kind == "session":
        store.update_side(job["id"], "A", {"session_id": "different-session"})
    elif kind == "live":
        monkeypatch.setattr(desk.runner, "is_side_live", lambda *_: True)
    elif kind == "demo_pending":
        store.update_side(job["id"], "A", {"demo": {**job["sides"]["A"]["demo"], "status": "running"}})
    else:
        relative = {"report": "demo/a/demo-A/report.json", "video": "demo/a/demo-A/video.mp4",
                    "commands": "auto-review/commands.json"}[kind]
        (store.evidence_dir(job["id"]) / relative).write_bytes(b"changed")
    with pytest.raises(RuntimeError):
        desk.auto_review_save(body)


def test_ai_draft_cannot_acquire_false_no_ai_attestation(ready_pair):
    _, desk, body = ready_pair
    desk.auto_review_save(body)
    with pytest.raises(RuntimeError, match="使用了授权 AI"):
        desk.review_save({"job": body["job"], "ai_confirmed": True, "lock": True})


def test_atomic_store_refuses_side_restart_or_user_decision(ready_pair):
    store, _, body = ready_pair
    before = store.get_job(body["job"])
    store.update_side(body["job"], "A", {"status": "running"})
    with pytest.raises(RuntimeError, match="证据已变化"):
        store.update_authorized_review(body["job"], {"reason": "changed"}, before["sides"])
    store.update_review(body["job"], {"validity": "有效"})
    with pytest.raises(RuntimeError, match="用户判断"):
        store.update_authorized_review(body["job"], {"reason": "changed"}, before["sides"])


def test_evidence_file_cannot_escape_job_directory(ready_pair, tmp_path):
    _, desk, body = ready_pair
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    body["evidence_files"] = [{"path": str(outside), "sha256": digest(outside)}]
    with pytest.raises(RuntimeError, match="必须属于本任务"):
        desk.auto_review_save(body)


def test_failed_demo_is_preserved_without_inventing_video(ready_pair):
    store, desk, body = ready_pair
    side = store.get_job(body["job"])["sides"]["A"]
    report = {**side["demo"], "status": "failed", "error": "启动失败，未录制成功"}
    folder = store.evidence_dir(body["job"]) / "demo" / "a" / "demo-A"
    (folder / "video.mp4").unlink()
    path = folder / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    store.update_side(body["job"], "A", {"demo": report, "video_local": ""})
    body["evidence_bindings"]["A"].update(demo_report_sha256=digest(path), video_sha256="")
    saved = desk.auto_review_save(body)
    assert saved["sides"]["A"]["demo"]["status"] == "failed"
    assert not saved["review"]["validity"]


@pytest.mark.parametrize("change", ["video_local", "trace_url", "video_url", "jsonl_bytes"])
@pytest.mark.parametrize("phase", ["save", "export"])
def test_current_upload_sources_cannot_diverge_from_evaluation(ready_pair, tmp_path, change, phase):
    store, desk, body = ready_pair
    if phase == "export":
        desk.auto_review_save(body)
        desk.review_save({"job": body["job"], "validity": "有效"})
    side = store.get_job(body["job"])["sides"]["A"]
    if change == "video_local":
        alternate = tmp_path / "unrelated.mp4"
        alternate.write_bytes(b"different-video")
        store.update_side(body["job"], "A", {"video_local": str(alternate)})
    elif change == "jsonl_bytes":
        from pathlib import Path
        Path(side["jsonl_local"]).write_bytes(b"different-trace")
    else:
        store.update_side(body["job"], "A", {change: "https://example.com/unrelated-evidence"})
    with pytest.raises(RuntimeError):
        if phase == "save":
            desk.auto_review_save(body)
        else:
            desk.review_export({"job": body["job"]})


def test_export_stays_strict_and_preserves_ai_source(ready_pair, monkeypatch):
    store, desk, body = ready_pair
    desk.auto_review_save(body)
    with pytest.raises(ValueError, match="未完成的必填项"):
        desk.review_export({"job": body["job"]})
    assert not store.get_job(body["job"])["exported"]
    desk.review_save({"job": body["job"], "validity": "有效"})
    calls = []

    def strict_export(job, path, *, strict):
        calls.append(strict)
        return {"tsv": "test"}

    monkeypatch.setattr(desk_mod, "export_tsv", strict_export)
    assert desk.review_export({"job": body["job"]}) == {"tsv": "test"}
    assert calls == [True]
    saved = store.get_job(body["job"])
    assert saved["exported"] and saved["review"]["auto_review"]["origin"] == "ai-authorized"
    assert saved["review"]["ai_confirmed"] is False and not saved["review"]["locked_at"]
    store.update_side(body["job"], "B", {"head_sha": "c" * 40})
    with pytest.raises(RuntimeError, match="绑定已变化"):
        desk.review_export({"job": body["job"]})


def test_original_human_review_still_requires_no_ai_attestation(ready_pair):
    store, desk, _ = ready_pair
    job = store.create_job({"prompt": "ordinary human review"})
    with pytest.raises(RuntimeError, match="未使用任何 AI"):
        desk.review_save({"job": job["id"], "reason": "人工评审理由。", "lock": True})
    saved = desk.review_save({"job": job["id"], "reason": "人工评审理由。", "lock": True, "ai_confirmed": True})
    assert saved["review"]["ai_confirmed"] and saved["review"]["locked_at"]


def test_live_ai_review_refresh_keeps_server_scores_and_real_user_edits():
    """Run the actual browser functions through the old-DOM/new-server race."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser JavaScript behavior check")
    page = desk_mod.PAGE
    draft_code = page[page.index("// ---- preserve in-progress GSB draft"):page.index("function recSavedHtml")]
    delivery_code = page[page.index("function deliveryFields()"):page.index("function copyTsv()")]
    save_code = page[page.index("async function saveReview(lock)"):page.index("async function unlockReview()")]
    export_code = page[page.index("async function exportRow()"):page.index("function deliveryFields()")]
    script = """
const assert=require('node:assert/strict');
let SEL='pair-example', JOBS=[], sent=[];
const window={__tsv:''}, document={activeElement:null}, elements={};
const $=id=>elements[id]||null;
const api=async(path,body)=>{sent.push({path,body});return {tsv:'test TSV'}};
const toast=()=>{}, refreshDetail=()=>{};
const keys=['validity','conclusion','reason','a_delivery_score','a_delivery_description','b_delivery_score','b_delivery_description'];
function put(review){for(const key of keys)elements['r_'+key]={id:'r_'+key,value:String(review[key]||''),selectionStart:2,selectionEnd:4,focus(){this.focused=true},setSelectionRange(start,end){this.selection=[start,end]}};
elements.r_aic={checked:!!review.ai_confirmed};elements.vA={value:''};elements.vB={value:''};
elements.tsvBox={style:{display:'none'}};elements.tsvPre={textContent:''};elements.promptCard={open:true,querySelector:()=>({scrollTop:0})};}
""" + draft_code + delivery_code + save_code + export_code + """
(async()=>{
  const old={};put(old);detailRenderedReview={job:SEL,values:reviewDraftValues(old)};
  const fresh={conclusion:'A 更好',reason:'服务器新填的完整理由',a_delivery_score:'4',a_delivery_description:'A真实描述',b_delivery_score:'3',b_delivery_description:'B真实描述',auto_review:{origin:'ai-authorized'}};
  JOBS=[{id:SEL,review:fresh}];
  // Polling skips DOM rendering while validity still has focus. Direct save
  // and export must merge the fresh server review with that genuine edit.
  $('r_validity').value='有效';await saveReview(false);
  assert.equal(sent[0].body.validity,'有效');assert.equal(sent[0].body.reason,fresh.reason);
  assert.equal(sent[0].body.a_delivery_score,'4');assert.equal(sent[0].body.b_delivery_description,fresh.b_delivery_description);
  sent=[];await exportRow();
  assert.equal(sent[0].path,'/api/review');assert.equal(sent[0].body.reason,fresh.reason);
  assert.equal(sent[0].body.b_delivery_score,'3');assert.equal(sent[1].path,'/api/export');
  sent=[];put(old);detailRenderedReview={job:SEL,values:reviewDraftValues(old)};
  // The poll already replaced JOBS, while the displayed DOM is still blank.
  captureDetailDraft();put(fresh);detailRenderedReview={job:SEL,values:reviewDraftValues(fresh)};restoreDetailDraft();
  for(const key of keys.filter(x=>x!=='validity'))assert.equal($('r_'+key).value,String(fresh[key]));
  $('r_validity').value='有效';await saveReview(false);
  assert.equal(sent[0].body.validity,'有效');assert.equal(sent[0].body.reason,fresh.reason);
  assert.equal(sent[0].body.a_delivery_score,'4');assert.equal(sent[0].body.b_delivery_description,fresh.b_delivery_description);
  assert.equal(sent[0].body.ai_confirmed,false);assert.equal(sent[0].body.lock,false);
  // Actual user edits, including deliberately clearing text, survive polling.
  $('r_reason').value='用户仍在输入的理由';$('r_a_delivery_description').value='';
  document.activeElement=$('r_reason');captureDetailDraft();
  const newer={...fresh,reason:'更新后的服务器理由',a_delivery_description:'更新后的A说明',b_delivery_score:'5'};
  put(newer);detailRenderedReview={job:SEL,values:reviewDraftValues(newer)};restoreDetailDraft();
  assert.equal($('r_reason').value,'用户仍在输入的理由');assert.equal($('r_a_delivery_description').value,'');
  assert.equal($('r_b_delivery_score').value,'5');assert.equal($('r_validity').value,'有效');
  assert.equal($('r_reason').focused,true);assert.deepEqual($('r_reason').selection,[2,4]);
  console.log('Browser refresh behavior passed');
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr

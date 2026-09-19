"""Strict TSV row export (paste into the Feishu table) and OSS upload orchestration."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import oss as oss_mod
from .checklist import run_checklist
from .desk_store import DeskStore

HEADERS = [
    "User Prompt", "提交人", "任务类型", "Harness", "任务难度", "语言/框架",
    "操作系统", "环境可复现等级", "初始环境快照",
    "A-SessionID", "A-轨迹文件", "A-产物快照", "A-运行录屏",
    "B-SessionID", "B-轨迹文件", "B-产物快照", "B-运行录屏",
    "有效性", "GSB 结论", "GSB 理由",
    "内部质检", "质检反馈", "备注",
]


def job_row(job: dict) -> list[str]:
    a, b = job["sides"]["A"], job["sides"]["B"]
    review = job.get("review", {})
    return [
        job.get("prompt", ""),
        review.get("reviewer", ""),
        job.get("task_type", ""),
        job.get("harness", ""),
        job.get("difficulty", ""),
        job.get("stack", ""),
        job.get("os_name", ""),
        job.get("repro_level", ""),
        job.get("baseline_url", ""),
        a.get("session_id", ""), a.get("trace_url", ""), a.get("head_url", ""), a.get("video_url", ""),
        b.get("session_id", ""), b.get("trace_url", ""), b.get("head_url", ""), b.get("video_url", ""),
        review.get("validity", ""),
        review.get("conclusion", ""),
        review.get("reason", ""),
        # QC-side columns (内部质检 / 质检反馈 / 备注): left blank for reviewers.
        "", "", "",
    ]


def export_tsv(job: dict, output: str | Path, *, strict: bool = True) -> dict:
    report = run_checklist(job, online=False)
    if strict and not report["ready"]:
        missing = [f"{x['group']}·{x['label']}" for x in report["items"] if x["blocking"] and not x["ok"]]
        raise ValueError("存在未完成的必填项，无法严格导出：\n- " + "\n- ".join(missing))
    buf = io.StringIO()
    writer = csv.writer(buf, dialect="excel-tab", lineterminator="\n")
    writer.writerow(HEADERS)
    writer.writerow(job_row(job))
    text = buf.getvalue()
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return {"path": str(out), "tsv": text, "checklist": report}


def upload_side(store: DeskStore, job: dict, side_name: str, cfg: oss_mod.OssConfig) -> dict:
    """Upload jsonl + video for one side. Idempotent; returns {trace_url, video_url}."""
    side = job["sides"][side_name]
    session_id = side.get("session_id", "")
    result: dict[str, str] = {}

    jsonl = side.get("jsonl_local", "")
    if jsonl and Path(jsonl).is_file() and session_id:
        key = f"{cfg.key_prefix}/{session_id}.jsonl"
        info = oss_mod.upload_file(cfg, jsonl, key)
        result["trace_url"] = info["url"]
        result["trace_size"] = str(info["size"])

    video = side.get("video_local", "")
    if video and Path(video).is_file():
        key = f"{cfg.key_prefix}/{session_id or job['id']}-{side_name}.mp4"
        info = oss_mod.upload_file(cfg, video, key)
        result["video_url"] = info["url"]
        result["video_size"] = str(info["size"])

    if not result:
        raise ValueError(f"{side_name} 侧没有可上传的轨迹/录屏")
    return result

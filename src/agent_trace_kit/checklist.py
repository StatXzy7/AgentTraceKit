"""Mechanical completeness checklist for one pair.

Every check is a deterministic rule over recorded facts — file existence,
40-char SHAs, git ancestry, URL shape. Nothing here analyses trajectory
content or judges model quality; the GSB decision stays entirely human.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .desk_store import CONCLUSIONS, DIFFICULTIES, TASK_TYPES
from . import workspace as ws

SHA_URL = re.compile(r"^https://[\w.-]+/[^/]+/[^/]+/commit/[0-9a-f]{40}$")
URL_RE = re.compile(r"^https?://")

VIDEO_MAX_SECONDS = 90.0


def video_duration_seconds(path: str | Path) -> float | None:
    """Return duration in seconds via ffprobe, or None if it cannot be measured."""
    if not shutil.which("ffprobe"):
        return None
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _check(items: list, cid: str, group: str, label: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
    items.append({"id": cid, "group": group, "label": label, "ok": bool(ok), "detail": detail, "blocking": blocking})


def _jsonl_user_texts(path: Path) -> list[str]:
    texts: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                msg = rec.get("message")
                content = msg.get("content") if isinstance(msg, dict) else msg
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    text = " ".join(
                        str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"
                    )
                text = text.strip()
                if text and not text.startswith("<"):
                    texts.append(text)
    except OSError:
        pass
    return texts


def _normalize_model(name: str) -> str:
    """auto_model/urm[1M] and auto_model/urm are the same base model."""
    return re.sub(r"\[[^\]]*\]", "", (name or "").strip())


def _jsonl_models(path: Path) -> list[str]:
    models: set[str] = set()
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = rec.get("message")
                if isinstance(msg, dict) and msg.get("model"):
                    models.add(str(msg["model"]))
    except OSError:
        pass
    return sorted(models)


def _side_checks(items: list, job: dict, side_name: str, online: bool) -> None:
    side = job["sides"][side_name]
    group = f"{side_name} 侧"
    wdir = side.get("workspace", "")
    _check(items, f"{side_name}_workspace", group, "独立工作区", bool(wdir) and Path(wdir).is_dir(),
           wdir or "未准备工作区")

    sid = side.get("session_id", "")
    _check(items, f"{side_name}_session", group, "SessionID 非空", bool(sid), sid or "缺失")

    jsonl = side.get("jsonl_local", "")
    jsonl_ok = bool(jsonl) and Path(jsonl).is_file() and Path(jsonl).stat().st_size > 0
    _check(items, f"{side_name}_jsonl", group, "轨迹 jsonl 存在且非空", jsonl_ok,
           jsonl if jsonl_ok else "未采集到轨迹文件")
    if jsonl_ok:
        user_texts = _jsonl_user_texts(Path(jsonl))
        prompt = job.get("prompt", "").strip()
        hit = any(prompt and prompt in t for t in user_texts)
        _check(items, f"{side_name}_prompt_match", group, "轨迹中的提示词与本题一致", hit,
               "已在轨迹用户消息中找到该 prompt" if hit else "轨迹里找不到本题 prompt，可能绑错了会话")
        _check(items, f"{side_name}_single_turn", group, "只有一轮有效交互（规范要求首轮）",
               len(user_texts) == 1, f"识别到 {len(user_texts)} 条用户消息", blocking=False)
        used_models = _jsonl_models(Path(jsonl))
        pinned = _normalize_model(ws.PINNED_MODEL)
        mismatched = [m for m in used_models if _normalize_model(m) != pinned]
        _check(items, f"{side_name}_model", group, f"使用指定模型（{ws.PINNED_MODEL}）",
               bool(used_models) and not mismatched,
               "轨迹记录模型：" + ", ".join(used_models) if used_models else "轨迹里读不到模型名",
               blocking=False)

    sha = side.get("head_sha", "")
    sha_ok = ws.is_sha40(sha)
    _check(items, f"{side_name}_sha", group, "产物快照 40 位 SHA", sha_ok, sha or "未提交/未采集")

    url = side.get("head_url", "")
    _check(items, f"{side_name}_url", group, "产物 commit permalink 格式", bool(SHA_URL.match(url)),
           url or "缺失")

    if sha_ok and ws.is_sha40(job.get("baseline_sha", "")) and wdir and Path(wdir).is_dir():
        ancestor = ws.is_ancestor(wdir, job["baseline_sha"], sha)
        _check(items, f"{side_name}_ancestry", group, "初始快照是该产物提交的祖先（同一起点）",
               ancestor, "祖先关系成立" if ancestor else "基线提交不是产物的祖先，起点不一致")
    else:
        _check(items, f"{side_name}_ancestry", group, "初始快照是该产物提交的祖先（同一起点）", False,
               "缺少基线或产物 SHA，无法核验")

    _check(items, f"{side_name}_pushed", group, "产物已 push 到远端", bool(side.get("pushed")),
           "已 push" if side.get("pushed") else "未 push 或未确认远端可达")

    trace_url = side.get("trace_url", "")
    _check(items, f"{side_name}_trace_url", group, "轨迹已上传并生成链接", bool(URL_RE.match(trace_url)),
           trace_url or "未上传")

    video_local = side.get("video_local", "")
    video_url = side.get("video_url", "")
    video_ready = bool(URL_RE.match(video_url)) or (bool(video_local) and Path(video_local).is_file())
    _check(items, f"{side_name}_video", group, "运行录屏已绑定/上传", video_ready,
           video_url or video_local or "缺失")
    if video_local and Path(video_local).is_file():
        size_mb = Path(video_local).stat().st_size / 1024 / 1024
        dur = video_duration_seconds(video_local)
        dur_ok = dur is None or dur <= VIDEO_MAX_SECONDS
        detail = f"{size_mb:.1f} MB"
        if dur is not None:
            detail += f"，时长 {dur:.0f}s"
            detail += "（≤90s 合规）" if dur_ok else "（超过规范 90 秒上限，请重录）"
        _check(items, f"{side_name}_video_duration", group, "录屏时长 ≤ 90 秒", dur_ok, detail,
               blocking=dur is not None and not dur_ok)
    if video_url and not video_local:
        _check(items, f"{side_name}_video_size", group, "录屏文件", True, "已上传（本地未留文件，时长请自行确认 ≤90s）",
               blocking=False)
    if online and URL_RE.match(trace_url):
        from . import oss
        h = oss.anonymous_head(trace_url)
        _check(items, f"{side_name}_trace_live", group, "轨迹链接可匿名访问", h.get("status") == 200,
               f"HTTP {h.get('status')} {h.get('error', '')}".strip(), blocking=False)
        if URL_RE.match(video_url):
            hv = oss.anonymous_head(video_url)
            _check(items, f"{side_name}_video_live", group, "录屏链接可匿名访问", hv.get("status") == 200,
                   f"HTTP {hv.get('status')} {hv.get('error', '')}".strip(), blocking=False)

    if side.get("status") == "failed":
        _check(items, f"{side_name}_run", group, "本次执行成功结束", False,
               side.get("error", "执行失败") + "（失败也必须保留证据；可重跑该侧）")


def run_checklist(job: dict, *, online: bool = False) -> dict:
    items: list = []

    _check(items, "prompt", "题目", "User Prompt 完整原文", bool(job.get("prompt", "").strip()),
           f"{len(job.get('prompt', ''))} 字符")
    _check(items, "task_type", "题目", "任务类型", job.get("task_type") in TASK_TYPES,
           job.get("task_type", "") or "缺失")
    _check(items, "difficulty", "题目", "任务难度（仅困难/地狱）", job.get("difficulty") in DIFFICULTIES,
           job.get("difficulty", "") or "缺失")
    _check(items, "stack", "题目", "语言/框架", bool(job.get("stack", "").strip()),
           job.get("stack", "") or "缺失")
    _check(items, "harness", "环境", "Harness 为 Claude Code", job.get("harness") == "Claude Code",
           job.get("harness", ""))
    _check(items, "harness_version", "环境", "Harness 版本", bool(job.get("harness_version")),
           job.get("harness_version", "") or "准备任务时自动采集")
    _check(items, "os", "环境", "操作系统", bool(job.get("os_name")), job.get("os_name", ""))
    _check(items, "repro", "环境", "环境可复现等级", bool(job.get("repro_level")),
           job.get("repro_level", "") or "选填")
    if not job.get("repro_level"):
        items[-1]["blocking"] = False

    baseline_sha = job.get("baseline_sha", "")
    _check(items, "baseline_sha", "初始快照", "40 位完整 SHA", ws.is_sha40(baseline_sha),
           baseline_sha or "未在基线仓库提交")
    baseline_url = job.get("baseline_url", "")
    _check(items, "baseline_url", "初始快照", "commit permalink", bool(SHA_URL.match(baseline_url)),
           baseline_url or "缺失")
    _check(items, "baseline_pushed", "初始快照", "已 push，评测方可访问", bool(job.get("baseline_pushed")),
           "已 push" if job.get("baseline_pushed") else "未确认")

    _side_checks(items, job, "A", online)
    _side_checks(items, job, "B", online)

    a = job["sides"]["A"]
    b = job["sides"]["B"]
    if a.get("session_id") and b.get("session_id"):
        _check(items, "sessions_distinct", "一致性", "A/B 是两个不同会话",
               a["session_id"] != b["session_id"], "会话 ID 不同")
    else:
        _check(items, "sessions_distinct", "一致性", "A/B 是两个不同会话", False, "等待两侧会话 ID")
    if ws.is_sha40(a.get("head_sha", "")) and ws.is_sha40(b.get("head_sha", "")):
        _check(items, "shas_distinct", "一致性", "A/B 产物提交不同",
               a["head_sha"] != b["head_sha"], "两个不同 SHA（同 SHA 需在备注说明）", blocking=False)

    review = job.get("review", {})
    _check(items, "conclusion", "GSB", "结论（A 更好 / Same / B 更好）",
           review.get("conclusion") in CONCLUSIONS, review.get("conclusion", "") or "未选择")
    reason = review.get("reason", "").strip()
    min_len = 80 if review.get("conclusion") == "Same" else 30
    reason_ok = len(reason) >= min_len
    _check(items, "reason", "GSB",
           f"理由（至少 {min_len} 字，Same 需更详细）", reason_ok,
           f"{len(reason)} 字" if reason else "未填写")
    _check(items, "reviewer", "GSB", "标注员", bool(review.get("reviewer", "").strip()),
           review.get("reviewer", "") or "缺失", blocking=False)

    # 凭据保护：两侧工作区的 .gitignore 应排除常见密钥文件
    for side_name in ("A", "B"):
        wd = job["sides"][side_name].get("workspace", "")
        if wd and Path(wd).is_dir():
            gi = Path(wd) / ".gitignore"
            text = gi.read_text(encoding="utf-8", errors="replace") if gi.exists() else ""
            protected = ".env" in text
            _check(items, f"{side_name}_gitignore", "安全", f"{side_name} 侧 .gitignore 排除 .env",
                   protected, "已排除" if protected else "未发现 .env 排除规则", blocking=False)

    blocking = [x for x in items if x["blocking"] and not x["ok"]]
    warnings = [x for x in items if not x["blocking"] and not x["ok"]]
    return {
        "ready": not blocking,
        "blocking_count": len(blocking),
        "warning_count": len(warnings),
        "items": items,
    }

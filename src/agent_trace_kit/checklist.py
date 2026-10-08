"""Mechanical completeness checklist for one pair.

Every check is a deterministic rule over recorded facts — file existence,
40-char SHAs, git ancestry, URL shape and actual tool dispatch receipts.
These checks do not replace semantic review; the GSB decision stays human.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

from .desk_store import REPRO_LEVELS, TASK_TYPES, VALIDITY
from . import workspace as ws
from . import engines

SHA_URL = re.compile(r"^https://[\w.-]+/[^/]+/[^/]+/commit/[0-9a-f]{40}$")
URL_RE = re.compile(r"^https?://")

# Videos run until the product demo ends; duration is recorded for information
# only and no longer blocks export (previously a hard 90-second cap).
VIDEO_RECORD_SECONDS = None


def video_duration_seconds(path: str | Path) -> float | None:
    """Return duration in seconds via ffprobe, or None if it cannot be measured."""
    if not shutil.which("ffprobe"):
        return None
    try:
        from .procmon import run_hidden
        p = run_hidden(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _check(items: list, cid: str, group: str, label: str, ok: bool, detail: str, *, blocking: bool = True) -> None:
    items.append({"id": cid, "group": group, "label": label, "ok": bool(ok), "detail": detail, "blocking": blocking})


def _user_record_text(rec: dict) -> str:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else msg
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            str(c.get("text", "")) for c in content if isinstance(c, dict) and c.get("type") == "text"
        ).strip()
    return ""


def _is_harness_user_noise(rec: dict, text: str) -> bool:
    """Claude Code injects extra type=user rows that are not a second human turn.

    Typical noise: Read(image) writes a ``turnCompanion`` caption
    ``[Image: original WxH…]`` with ``isMeta: true``; sidechain/Task prompts;
    ``<command-name>`` / ``<system-reminder>`` wrappers.
    """
    # Desk injects the frozen prompt via `claude -p` (promptSource=sdk).
    # Never drop that row even if later CLI flags are noisy.
    if rec.get("promptSource") == "sdk":
        return not bool(text)
    if rec.get("isMeta") or rec.get("turnCompanion") or rec.get("isSidechain"):
        return True
    if not text or text.startswith("<"):
        return True
    if text.startswith("[Image:"):
        return True
    return False


def _jsonl_user_texts(path: Path) -> list[str]:
    """Prompt-like user texts in a transcript (one item == one human/SDK turn)."""
    texts: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "user":
                    continue
                text = _user_record_text(rec)
                if _is_harness_user_noise(rec, text):
                    continue
                texts.append(text)
    except OSError:
        pass
    return texts


def _normalize_model(name: str) -> str:
    """auto_model/urm[1M] and auto_model/urm are the same base model.

    Stream transcripts also tag auxiliary/title turns as ``<synthetic>`` or
    ``<synthetic-name>``; strip the wrapper so those records don't read as a
    different model.
    """
    s = re.sub(r"\[[^\]]*\]", "", (name or "").strip())
    s = s.replace("<synthetic>", "").replace("</synthetic>", "")
    return re.sub(r"^<[^>]*>|</[^>]*>$", "", s).strip()


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


def _side_checks(items: list, job: dict, side_name: str, online: bool) -> set[str]:
    side = job["sides"][side_name]
    group = f"{side_name} 侧"
    used_models = []
    # Engineering-failure pairs are kept for audit, so a truncated/failed run
    # only blocks export for pairs still marked valid.
    voided = job.get("review", {}).get("validity", "") not in ("", "有效")
    wdir = side.get("workspace", "")
    _check(items, f"{side_name}_workspace", group, "独立工作区", bool(wdir) and Path(wdir).is_dir(),
           wdir or "未准备工作区")
    if job.get("baseline_sha"):
        initial = side.get("initial_sha", "")
        _check(items, f"{side_name}_initial_sha", group, "工作区从冻结 commit 创建",
               ws.is_sha40(initial) and initial == job.get("baseline_sha"),
               initial or "缺少工作区初始 SHA，需重新准备该侧")

    sid = side.get("session_id", "")
    _check(items, f"{side_name}_session", group, "SessionID 非空", bool(sid), sid or "缺失")

    jsonl = side.get("jsonl_local", "")
    jsonl_ok = bool(jsonl) and Path(jsonl).is_file() and Path(jsonl).stat().st_size > 0
    _check(items, f"{side_name}_jsonl", group, "轨迹 jsonl 存在且非空", jsonl_ok,
           jsonl if jsonl_ok else "未采集到轨迹文件")
    if jsonl_ok:
        from .compliance import audit_trace, trace_detail, resolved_report
        compliance = audit_trace(jsonl)
        _check(items, f"{side_name}_extra_ai", group, "原轨迹没有额外做题 AI 派发",
               compliance["reason"] is None or resolved_report(compliance, side), trace_detail(compliance), blocking=not voided)
        if compliance["sessions"]:
            _check(items, f"{side_name}_raw_session", group, "原轨迹只属于本侧绑定会话",
                   compliance["sessions"] == [sid], ", ".join(compliance["sessions"]))
        codex = engines.job_engine(job) == "codex"
        recovered = codex and bool(side.get("completion_recovery"))
        evidence = engines.codex_evidence(jsonl, prompt=job.get("prompt", ""),
                                         allow_recovered_turns=recovered) if codex else {}
        user_texts = evidence["users"] if codex else _jsonl_user_texts(Path(jsonl))
        prompt = job.get("prompt", "").strip()
        if codex:
            prompt = engines.normalized_prompt(prompt)
        hit = (bool(prompt) and prompt in user_texts) if codex else any(prompt and prompt in t for t in user_texts)
        _check(items, f"{side_name}_prompt_match", group, "轨迹中的提示词与本题一致", hit,
               "已在轨迹用户消息中找到该 prompt" if hit else "轨迹里找不到本题 prompt，可能绑错了会话")
        turns = evidence.get("user_turn_count", len(user_texts))
        _check(items, f"{side_name}_single_turn", group, "生产单提示词协议（不作为官方轮数质检规则）",
               turns == 1, f"识别到 {turns} 轮用户交互", blocking=bool(job.get("execution_policy")))
        used_models = evidence["models"] if codex else _jsonl_models(Path(jsonl))
        expected = job.get("codex_model", "") if codex else job.get("claude_model") or ws.PINNED_MODEL
        mismatched = [m for m in used_models if expected and _normalize_model(m) != _normalize_model(expected)]
        _check(items, f"{side_name}_model", group, f"使用指定模型（{expected}）" if expected else "记录 Codex 实际模型（继承本机配置）",
               bool(used_models) and not mismatched,
               "轨迹记录模型：" + ", ".join(used_models) if used_models else "轨迹里读不到模型名",
               blocking=bool(job.get("codex_model" if codex else "claude_model")) and not voided)

        # Gateway cuts mid-turn leave a detectable tail: a synthetic "API Error"
        # final message (e.g. seed-code 504) or a transcript ending right after a
        # tool_result. Such evidence must never be exported as a valid product.
        if codex:
            _check(items, f"{side_name}_session_match", group, "轨迹 SessionID 与绑定一致",
                   evidence.get("session_id") == sid, str(evidence.get("session_id") or "缺失"))
        reason = evidence["reason"] if codex else ws.transcript_interruption_reason(Path(jsonl))
        reason_labels = {
            "api_error": "轨迹末尾是网关报错（API Error，如 504 断流），本轮被中途截断",
            "dangling_tool_result": "轨迹停在工具返回后、没有模型收尾，本轮被中途截断",
            "no_assistant": "轨迹里没有任何模型回复，本轮未真正执行",
            "unreadable": "轨迹文件无法读取",
            "malformed_json": "轨迹含损坏或截断的 JSONL，不能作为完整证据",
            "missing_completion": "Codex rollout 缺少轮次完成标记",
            "turn_failed": "Codex 轮次失败或被中止",
            "extra_ai_dispatched": "已派发额外做题 AI，主 CLI 结束不能证明合规",
            "extra_ai_needs_review": "额外 AI 调用缺少明确结果或 shell 模型调用需核对业务用途",
        }
        _check(items, f"{side_name}_trace_complete", group,
               "续接末轮完整（前序失败保留，不等同于首轮完成）" if recovered else "轨迹完整（首轮未被网关截断）",
               reason is None,
               reason_labels.get(reason, "末轮完成，前序尝试见原始轨迹" if recovered else "首轮完整，最后一次工具调用后有模型收尾"),
               blocking=not voided)
        if recovered and evidence.get("historical_unresolved_tool_calls"):
            _check(items, f"{side_name}_interrupted_tools", group, "前序中断工具调用已保留（需复核副作用）",
                   False, ", ".join(evidence["historical_unresolved_tool_calls"]), blocking=False)

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
        detail = f"{size_mb:.1f} MB"
        if dur is not None:
            detail += f"，时长 {dur:.0f}s（录到运行结束即可）"
        _check(items, f"{side_name}_video_duration", group, "录屏可正常读取时长", dur is not None or bool(URL_RE.match(video_url)),
               detail, blocking=False)
    if video_url and not video_local:
        _check(items, f"{side_name}_video_size", group, "录屏文件", True, "已上传（本地未留文件）",
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
        # A pair declared void (e.g. engineering failure) is still exported for
        # audit with whatever evidence exists, so a failed run does not block it.
        _check(items, f"{side_name}_run", group, "本次执行成功结束", False,
               side.get("error", "执行失败") + "（失败也必须保留证据；可重跑该侧）",
               blocking=not voided)


    return {_normalize_model(model) for model in used_models}


def run_checklist(job: dict, *, online: bool = False) -> dict:
    items: list = []

    _check(items, "prompt", "题目", "User Prompt 完整原文", bool(job.get("prompt", "").strip()),
           f"{len(job.get('prompt', ''))} 字符")
    _check(items, "task_type", "题目", "任务类型", job.get("task_type") in TASK_TYPES,
           job.get("task_type", "") or "缺失")
    _check(items, "difficulty", "题目", "任务难度已填写（实际难度需人工核对）", bool(job.get("difficulty")),
           job.get("difficulty", "") or "缺失")
    _check(items, "stack", "题目", "语言/框架", bool(job.get("stack", "").strip()),
           job.get("stack", "") or "缺失")
    _check(items, "harness", "环境", "Harness 为 Claude Code / Codex CLI", job.get("harness") in engines.ENGINE_LABELS.values(),
           job.get("harness", ""))
    _check(items, "harness_version", "环境", "Harness 版本", bool(job.get("harness_version")),
           job.get("harness_version", "") or "准备任务时自动采集")
    _check(items, "os", "环境", "操作系统", bool(job.get("os_name")), job.get("os_name", ""))
    _check(items, "repro", "环境", "环境可复现等级", job.get("repro_level") in REPRO_LEVELS,
           job.get("repro_level", "") or "缺失")

    baseline_sha = job.get("baseline_sha", "")
    if job.get("source_mode") == "github_commit":
        requested = job.get("source_commit", "")
        _check(items, "baseline_requested_commit", "初始快照", "冻结 SHA 与指定 commit 一致",
               bool(re.fullmatch(r"[0-9a-fA-F]{7,40}", requested))
               and ws.is_sha40(baseline_sha) and baseline_sha.lower().startswith(requested.lower()),
               f"指定：{requested}；冻结：{baseline_sha or '尚未准备'}")
    _check(items, "baseline_sha", "初始快照", "40 位完整 SHA", ws.is_sha40(baseline_sha),
           baseline_sha or "未在基线仓库提交")
    baseline_url = job.get("baseline_url", "")
    _check(items, "baseline_url", "初始快照", "commit permalink", bool(SHA_URL.match(baseline_url)),
           baseline_url or "缺失")
    _check(items, "baseline_pushed", "初始快照", "已 push，评测方可访问", bool(job.get("baseline_pushed")),
           "已 push" if job.get("baseline_pushed") else "未确认")

    a_models = _side_checks(items, job, "A", online)
    b_models = _side_checks(items, job, "B", online)
    _check(items, "models_same", "一致性", "A/B 原轨迹中的实际模型一致",
           bool(a_models and b_models) and a_models == b_models,
           f"A: {', '.join(sorted(a_models)) or '未识别'}; B: {', '.join(sorted(b_models)) or '未识别'}",
           blocking=job.get("review", {}).get("validity", "") in ("", "有效"))

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
    validity = review.get("validity", "")
    _check(items, "validity", "GSB", "有效性（有效 / 作废-…）",
           validity in VALIDITY, validity or "未选择")
    # A voided pair is recorded for audit but excluded from evaluation, so the
    # preference judgement is not required; for a valid pair it blocks export.
    is_valid_pair = validity == "有效"
    _check(items, "conclusion", "GSB", "GSB 结论已填写（含义与理由需一致）",
           bool(str(review.get("conclusion", "")).strip()), review.get("conclusion", "") or "未选择",
           blocking=is_valid_pair)
    reason = review.get("reason", "").strip()
    reason_ok = bool(reason)
    _check(items, "reason", "GSB",
           "理由已填写（比较依据及权衡需人工核对）", reason_ok,
           f"{len(reason)} 字" if reason else "未填写",
           blocking=is_valid_pair)
    from .compliance import review_issues
    issues = review_issues(job)
    _check(items, "review_consistency", "GSB", "结论侧别与题面引用无已检出冲突",
           not issues, "；".join(x["detail"] for x in issues) or "窄范围检查未检出冲突；不等同于完整语义质审",
           blocking=is_valid_pair)
    for side_name, prefix in (("A", "a"), ("B", "b")):
        score = str(review.get(f"{prefix}_delivery_score", "")).strip()
        description = str(review.get(f"{prefix}_delivery_description", "")).strip()
        required = bool(job.get("delivery_quality_required")) and is_valid_pair
        _check(items, f"{prefix}_delivery_score", "交付完整性",
               f"{side_name} - 交付完整性（1-5）", score in {"1", "2", "3", "4", "5"},
               score or "未评分", blocking=required)
        _check(items, f"{prefix}_delivery_description", "交付完整性",
               f"{side_name} - 交付完整性描述", bool(description),
               f"{len(description)} 字" if description else "未填写", blocking=required)
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

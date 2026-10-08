"""Evidence-based gates for formal pair runs; never rewrite raw transcripts.

Only actual tool calls are inspected. A successful dispatch remains a violation
even when its review report never arrives. Shell commands are review candidates,
not proof that a task's own AI product used an extra solving assistant.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

POLICY_VERSION = "2026-10-04-single-agent-v1"
PROTOCOL = (
    "Formal A/B evaluation: complete this task yourself in this session. "
    "Do not delegate implementation, analysis, or review to another AI agent. "
    "Do not launch an external model CLI/API for help solving or reviewing this task. "
    "Local task tracking, ordinary shell commands, tests, and the AI functionality "
    "explicitly requested by the product specification are allowed. "
    "Keep the original task requirements; do not invent environment assumptions. "
    "Report actual failures and incomplete work accurately."
)
CODEX_OVERRIDES = [
    "-c", "features.multi_agent=false", "-c", "features.multi_agent_v2=false",
    "-c", "developer_instructions=" + json.dumps(PROTOCOL),
]
CLAUDE_ALLOWED_TOOLS = {
    "Bash", "Edit", "Glob", "Grep", "Read", "Write", "NotebookEdit", "WebFetch", "WebSearch",
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskOutput", "TaskStop",
}
CLAUDE_FLAGS = [
    "--tools", ",".join(sorted(CLAUDE_ALLOWED_TOOLS)),
    "--disallowedTools", "Agent,Task", "--strict-mcp-config",
    "--mcp-config", '{"mcpServers":{}}', "--append-system-prompt", PROTOCOL,
]
_DELEGATES = {"agent", "task", "spawn_agent", "sendmessage", "teamcreate", "workflow",
              "mcp__codex_app__create_thread",
              "mcp__codex_app__fork_thread", "mcp__codex_app__send_message_to_thread"}
_SHELLS = {"bash", "exec_command", "run_command", "run_shell_command", "shell"}
_MODEL_COMMAND = re.compile(
    r"(?:^|[;&|\n]\s*|\b(?:exec|command)\s+)\s*(?:&\s*)?"
    r"(?:[\w./\\:\-]*?(?:codex|claude|gemini|aider)(?:\.cmd|\.exe)?|"
    r"[\"'][^\"'\r\n]*[/\\](?:codex|claude|gemini|aider)(?:\.cmd|\.exe)?[\"'])\s+"
    r"[^;&|\r\n]*?(?:\bexec\b|\breview\b|-p\b|--print\b|--prompt\b|--message\b)", re.I,
)
_API_COMMAND = re.compile(r"(?:curl|Invoke-RestMethod|requests\.(?:post|request))"
                          r"[\s\S]*?(?:/chat/completions|/responses|/messages)|"
                          r"\b\w+(?:\.\w+)*\.(?:responses|messages|chat\.completions)\.create\s*\(", re.I)


def _object(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _name(name: str) -> str:
    return str(name).rsplit(".", 1)[-1].lower()


def _result_status(value, is_error=False) -> str:
    data = _object(value)
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    # Denials must have no receipt of dispatch. A later failed child does not
    # undo an earlier successful launch.
    if data.get("agent_id") or data.get("agentId") or data.get("threadId") or data.get("clientThreadId") or data.get("status") in {
        "async_launched", "completed", "running",
    }:
        return "dispatched"
    if re.search(r"agentId:\s*\S+|agent_id[\"']?\s*[:=]\s*[\"']?\w+", text):
        return "dispatched"
    if re.search(r"Agent terminated", text, re.I):
        return "dispatched"
    if re.search(
        r"permission denied|blocked by policy|not allowed|tool.*(?:disabled|unavailable)|"
        r"No such tool|rejected|拒绝|禁止使用", text, re.I,
    ):
        return "blocked"
    return "unresolved"


def audit_trace(path: str | Path) -> dict:
    """Return immutable identity, physical line numbers and separate statuses."""
    calls: dict[str, dict] = {}
    reports: dict[str, int] = {}
    sessions: set[str] = set()
    digest = hashlib.sha256()
    error = ""
    try:
        with Path(path).open("rb") as handle:
            for number, raw in enumerate(handle, 1):
                digest.update(raw)
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8-sig"))
                    if not isinstance(record, dict):
                        raise ValueError("record is not an object")
                except (ValueError, UnicodeError):
                    error = f"malformed_json:line={number}"
                    break
                kind = record.get("type")
                payload = _object(record.get("payload"))
                if kind == "session_meta" and payload.get("id"):
                    sessions.add(str(payload["id"]))
                elif record.get("sessionId") and not record.get("isSidechain"):
                    sessions.add(str(record["sessionId"]))
                events = []
                if kind == "assistant":
                    content = _object(record.get("message")).get("content", [])
                    if isinstance(content, list):
                        events.extend(c for c in content if isinstance(c, dict) and c.get("type") == "tool_use")
                elif kind == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
                    events.append({"id": payload.get("call_id"), "name": payload.get("name"),
                                   "input": payload.get("arguments", payload.get("input", {}))})
                for event in events:
                    name = _name(event.get("name", ""))
                    args = _object(event.get("input"))
                    shell = str(args.get("cmd", args.get("command", "")))
                    candidate = name in _SHELLS and bool(_MODEL_COMMAND.search(shell) or _API_COMMAND.search(shell))
                    # functions.exec is an orchestration tool, not a new AI.
                    # A real spawn invocation embedded in its executable input
                    # is a candidate; example strings are never confirmed here.
                    if name == "exec" and isinstance(event.get("input"), str):
                        candidate = bool(re.search(r"\b(?:tools|collaboration)\.(?:spawn_agent|mcp__codex_app__(?:create_thread|fork_thread|send_message_to_thread))\s*\(", event["input"]))
                    if name not in _DELEGATES and not candidate:
                        continue
                    ident = str(event.get("id") or f"line-{number}")
                    calls[ident] = {"call_id": ident, "tool": event.get("name", ""),
                                    "line": number, "kind": "delegate" if name in _DELEGATES else "external_ai_candidate",
                                    "status": "unresolved", "result_line": None, "report_line": None}
                if kind == "user":
                    content = _object(record.get("message")).get("content", [])
                    if isinstance(content, list):
                        for result in content:
                            if isinstance(result, dict) and result.get("type") == "tool_result":
                                call = calls.get(str(result.get("tool_use_id")))
                                if call:
                                    receipt = record.get("toolUseResult", result.get("content", ""))
                                    call.update(status=_result_status(receipt, result.get("is_error", False)), result_line=number)
                                    if (call["status"] == "dispatched" and _object(receipt).get("status") == "completed"
                                            and (_object(receipt).get("content") or result.get("content"))):
                                        call["report_line"] = number
                elif kind == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                    call = calls.get(str(payload.get("call_id")))
                    if call:
                        call.update(status=_result_status(payload.get("output", "")), result_line=number)
                attachment = _object(record.get("attachment"))
                notification = attachment.get("prompt", "") if attachment.get("type") == "queued_command" else ""
                if kind == "user":
                    content = _object(record.get("message")).get("content", "")
                    if isinstance(content, str):
                        notification += content
                    elif isinstance(content, list):
                        notification += "".join(str(b.get("text", "")) for b in content if isinstance(b, dict) and b.get("type") == "text")
                if isinstance(notification, str) and "<task-notification>" in notification:
                    ident = re.search(r"<tool-use-id>(.*?)</tool-use-id>", notification, re.S)
                    status = re.search(r"<status>(.*?)</status>", notification, re.S)
                    result = re.search(r"<result>(.*?)</result>", notification, re.S)
                    if ident and status and status[1] == "completed" and result and result[1].strip():
                        reports[ident[1]] = number
        for ident, number in reports.items():
            if ident in calls:
                calls[ident].update(status="dispatched", report_line=number)
    except OSError:
        error = "unreadable"
    rows = list(calls.values())
    confirmed = [c for c in rows if c["kind"] == "delegate" and c["status"] == "dispatched"]
    pending = [c for c in rows if c["status"] != "blocked" and c not in confirmed]
    return {"policy_version": POLICY_VERSION, "sha256": digest.hexdigest() if not error else "",
            "sessions": sorted(sessions), "calls": rows, "error": error,
            "status": "not_verified" if error else "issue" if confirmed else "needs_review" if pending else "clear",
            "reason": error.partition(":")[0] if error else ("extra_ai_dispatched" if confirmed else "extra_ai_needs_review" if pending else None)}


def trace_detail(report: dict) -> str:
    if report["error"]:
        return report["error"]
    if not report["calls"]:
        return "原轨迹未检出额外 AI 调用；语义质审仍需人工完成"
    return "; ".join(f"{c['tool']} line {c['line']} -> {c['status']}"
                     f" (return {c['result_line']}, report {c['report_line']})" for c in report["calls"])


def completion_reason(report: dict) -> str | None:
    # Shell model/API commands need human interpretation against the task.
    # They are held at lock/export, not mislabelled as a truncated CLI turn.
    if report["error"] or any(c["kind"] == "delegate" and c["status"] != "blocked" for c in report["calls"]):
        return report["reason"]
    return None


def resolved_report(report: dict, side: dict) -> bool:
    decision = side.get("external_ai_review", {})
    candidates = [c for c in report["calls"] if c["status"] != "blocked"]
    return (not report["error"] and bool(candidates)
            and all(c["kind"] == "external_ai_candidate" for c in candidates)
            and decision.get("trace_sha256") == report["sha256"]
            and decision.get("call_ids") == [c["call_id"] for c in candidates]
            and decision.get("classification") == "product_functionality"
            and bool(str(decision.get("explanation", "")).strip())
            and bool(str(decision.get("reviewer", "")).strip()))


def evidence_binding(job: dict) -> dict:
    return {"policy_version": POLICY_VERSION, "baseline_sha": job.get("baseline_sha", ""),
            "prompt_sha256": hashlib.sha256(str(job.get("prompt", "")).encode()).hexdigest(),
            "sides": {name: {"session_id": side.get("session_id", ""), "head_sha": side.get("head_sha", ""),
                             "trace_sha256": audit_trace(side["jsonl_local"])["sha256"] if side.get("jsonl_local") else ""}
                      for name, side in job.get("sides", {}).items()}}


def review_issues(job: dict) -> list[dict]:
    """Narrow factual/side checks. No claim of full semantic quality review."""
    review = job.get("review", {})
    reason = str(review.get("reason", ""))
    issues = []
    # Only the explicit final preference, not mentions of a side's advantages.
    choices = re.findall(r"(?:综合(?:看|来看|考虑|而言)?|最终|综上|还是选择|所以)[，,:：\s]*"
                         r"(?:(?:还是|我|我们|选择|选择了|更倾向于|认为|倾向于|会|觉得)[，,:：\s]*)*"
                         r"([AB])\s*(?:更好|略好|更优)", reason)
    selected = {"A 更好": "A", "B 更好": "B", "Same": "Same"}.get(review.get("conclusion"))
    if choices and selected and choices[-1] != selected:
        issues.append({"field": "GSB", "status": "issue", "detail": "GSB 最终偏好与结论字段相反，请核对侧别"})
    premise = re.search(r"(?:主要求|题面|题目)(?:[^。\n]{0,12})(?:是|要求|规定|明确)(?:[^。\n]{0,8})utf[ -]?8", reason, re.I)
    if (not re.search(r"utf[ -]?8", str(job.get("prompt", "")), re.I) and premise
            and not re.search(r"不是|并非|未要求|未规定|没有要求|不要求", premise[0])):
        issues.append({"field": "GSB", "status": "needs_review", "detail": "GSB 引用了题面未出现的 UTF-8 前提，请回查题面并修正或补充依据"})
    return issues


def require_review_compliance(job: dict) -> None:
    """Shared by the server lock and strict export; voided evidence is retained."""
    if job.get("review", {}).get("validity") != "有效":
        return
    for side_name, side in job.get("sides", {}).items():
        if not side.get("jsonl_local"):
            raise ValueError(f"{side_name} 侧缺少原轨迹，无法锁定或导出有效标注")
        report = audit_trace(side["jsonl_local"])
        if report["reason"] and not resolved_report(report, side):
            raise ValueError(f"{side_name} 侧轨迹合规检查未通过：{trace_detail(report)}")
        if report["sessions"] and report["sessions"] != [side.get("session_id", "")]:
            raise ValueError(f"{side_name} 侧原轨迹会话与绑定不一致")
    previous = job.get("review", {}).get("evidence_binding")
    if previous and previous != evidence_binding(job):
        raise ValueError("评审锁定后原轨迹或题面/快照绑定已变化，请重新核验并解锁再锁定")
    issues = review_issues(job)
    if issues:
        raise ValueError("；".join(x["detail"] for x in issues))


def main() -> None:
    """Read-only batch audit with statuses; this never declares semantic PASS."""
    import argparse
    parser = argparse.ArgumentParser(description="Audit raw pair trajectories without modifying them")
    parser.add_argument("--jobs-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    rows = []
    for path in sorted(args.jobs_dir.glob("*.json")):
        job = json.loads(path.read_text(encoding="utf-8-sig"))
        rows.append({"job_id": job.get("id"), "name": job.get("name"),
                     "review_issues": review_issues(job),
                     "sides": {name: audit_trace(s["jsonl_local"]) if s.get("jsonl_local") else {"status": "not_verified", "reason": "missing_trace"}
                               for name, s in job.get("sides", {}).items()}})
    from .run_policy import private_write
    private_write(args.output, json.dumps({"policy_version": POLICY_VERSION, "semantic_review": "NOT_RUN", "jobs": rows}, ensure_ascii=False, indent=2))
    print(f"Audited {len(rows)} jobs; report saved. Semantic review remains required.")


if __name__ == "__main__":
    main()

"""CLI selection and Codex rollout evidence for the Pair Desk."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

ENGINE_LABELS = {"claude": "Claude Code", "codex": "Codex CLI"}
CODEX_SANDBOXES = ("workspace-write", "read-only", "danger-full-access")


def engine_id(value: str | None = None) -> str:
    aliases = {label.lower(): key for key, label in ENGINE_LABELS.items()}
    name = str(value or "claude").strip().lower()
    name = aliases.get(name, name)
    if name not in ENGINE_LABELS:
        raise ValueError("运行 CLI 必须是 Claude Code 或 Codex CLI")
    return name


def job_engine(job: dict) -> str:
    # Missing fields in historical jobs always mean Claude, never today's default.
    return engine_id(job.get("agent") or job.get("harness"))


def cli_command(settings: dict, agent: str) -> str:
    command = str(settings.get(f"{agent}_command") or agent).strip().strip('"')
    return shutil.which(command) or command


def cli_version(settings: dict, agent: str) -> str:
    from .procmon import run_hidden

    try:
        result = run_hidden(
            [cli_command(settings, agent), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def codex_args(settings: dict, job: dict) -> list[str]:
    from .compliance import CODEX_OVERRIDES
    sandbox = settings.get("codex_sandbox", "workspace-write")
    if sandbox not in CODEX_SANDBOXES:
        raise ValueError("Codex sandbox 配置无效")
    # Never put a prompt into a Windows .cmd command line. stdin preserves
    # quotes, shell metacharacters, Unicode and prompts larger than argv limits.
    args = [
        cli_command(settings, "codex"),
        "exec",
        "--json",
        "--color",
        "never",
        "--sandbox",
        sandbox,
        "-c",
        'approval_policy="never"',
    ]
    args.extend(CODEX_OVERRIDES)
    if job.get("codex_model"):
        args.extend(["--model", job["codex_model"]])
    return [*args, "-"]


def _records(path: str | Path):
    with Path(path).open(encoding="utf-8-sig") as handle:
        for line in handle:
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("JSONL record is not an object")
                yield record


def codex_resume_args(settings: dict, job: dict, session_id: str) -> list[str]:
    """Resume a bound session; resume accepts config overrides, not --sandbox."""
    sandbox = settings.get("codex_sandbox", "workspace-write")
    if sandbox not in CODEX_SANDBOXES:
        raise ValueError("Codex sandbox 配置无效")
    args = [cli_command(settings, "codex"), "exec", "resume", "--json",
            "-c", 'approval_policy="never"', "-c", "sandbox_mode=" + json.dumps(sandbox)]
    from .compliance import CODEX_OVERRIDES
    args.extend(CODEX_OVERRIDES)
    if job.get("codex_model"):
        args.extend(["--model", job["codex_model"]])
    return [*args, session_id, "-"]


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return ""


def normalized_prompt(text: str) -> str:
    """Canonical line endings for Windows pipes; raw evidence is never rewritten."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def codex_evidence(path: str | Path, *, prompt: str = "", allow_recovered_turns: bool = False) -> dict:
    """Read authoritative user turns, model provenance and terminal state.

    event_msg user_message is the human input; response_item copies also contain
    AGENTS.md/environment context. Never count these injected messages as turns.
    Completion must follow a closing answer and all outstanding tool calls.
    """
    users: list[str] = []
    fallback_users: list[str] = []
    models: set[str] = set()
    pending: set[str] = set()
    historical_pending: set[str] = set()
    sid = cwd = ""
    closer = completed = False
    failure = ""
    turn_ids: set[str] = set()
    anonymous_starts = {"task_started": 0, "turn_started": 0}
    try:
        for rec in _records(path):
            kind = rec.get("type")
            payload = rec.get("payload") or {}
            if not isinstance(payload, dict):
                return {
                    "reason": "malformed_json",
                    "users": users,
                    "models": sorted(models),
                }
            if kind == "session_meta":
                sid = payload.get("id") or payload.get("session_id") or ""
                cwd = payload.get("cwd") or ""
            elif kind == "turn_context" and payload.get("model"):
                models.add(str(payload["model"]))
            elif kind == "event_msg":
                event = payload.get("type")
                if event in anonymous_starts:
                    if payload.get("turn_id"):
                        turn_ids.add(str(payload["turn_id"]))
                    else:
                        anonymous_starts[event] += 1
                if event in (
                    "task_started",
                    "turn_started",
                    "user_message",
                    "user_message_event",
                ):
                    completed = closer = False
                    failure = ""
                    if allow_recovered_turns and event in ("task_started", "turn_started"):
                        historical_pending.update(pending)
                        pending.clear()
                if event in ("user_message", "user_message_event"):
                    if allow_recovered_turns and users:
                        # A cancelled earlier turn remains incomplete evidence.
                        # Evaluate the new turn separately, exposing the old
                        # unresolved calls rather than pretending they finished.
                        historical_pending.update(pending)
                        pending.clear()
                    users.append(
                        normalized_prompt(
                            _content_text(
                                payload.get("message")
                                or payload.get("text")
                                or payload.get("content")
                            )
                        )
                    )
                elif event in (
                    "task_complete",
                    "turn_complete",
                    "task_completed",
                    "turn_completed",
                ):
                    completed = not bool(payload.get("error"))
                    if not completed:
                        failure = "turn_failed"
                    closer = closer or bool(payload.get("last_agent_message"))
                elif event == "model_reroute" and payload.get("to_model"):
                    models.add(str(payload["to_model"]))
                elif event in ("turn_aborted", "task_failed", "turn_failed", "error"):
                    failure = "turn_failed"
                    completed = False
            elif kind == "response_item":
                nested = payload.get("item")
                item = nested if isinstance(nested, dict) else payload
                itype = item.get("type")
                if itype == "message" and item.get("role") == "user":
                    text = normalized_prompt(_content_text(item.get("content")))
                    context = text.startswith(
                        (
                            "# AGENTS.md",
                            "<environment_context>",
                            "<permissions instructions>",
                            "<recommended_plugins>",
                            "<skills_instructions>",
                        )
                    )
                    if text and (not context or text == normalized_prompt(prompt)):
                        if allow_recovered_turns and fallback_users:
                            historical_pending.update(pending)
                            pending.clear()
                            failure = ""
                        fallback_users.append(text)
                        completed = closer = False
                elif itype == "message" and item.get("role") == "assistant":
                    if item.get("phase") in (None, "final_answer"):
                        closer = bool(_content_text(item.get("content")))
                elif itype in ("function_call", "custom_tool_call", "tool_call"):
                    pending.add(str(item.get("call_id") or item.get("id") or "unknown"))
                    closer = completed = False
                elif itype in (
                    "function_call_output",
                    "custom_tool_call_output",
                    "tool_result",
                ):
                    pending.discard(
                        str(item.get("call_id") or item.get("id") or "unknown")
                    )
                    historical_pending.discard(str(item.get("call_id") or item.get("id") or "unknown"))
    except OSError:
        failure = "unreadable"
    except (ValueError, UnicodeError):
        failure = "malformed_json"
    reason = failure or (
        "dangling_tool_result"
        if pending
        else (
            "no_assistant"
            if not closer
            else "missing_completion" if not completed else None
        )
    )
    # Both record formats can occur in one rollout. Match redundant copies by
    # occurrence, but retain response-only follow-ups instead of hiding them.
    unmatched = list(users)
    merged_users = list(users)
    for text in fallback_users:
        if text in unmatched:
            unmatched.remove(text)
        else:
            merged_users.append(text)
    from .compliance import audit_trace, completion_reason
    compliance = audit_trace(path)
    return {
        "reason": completion_reason(compliance) or reason,
        "compliance": compliance,
        "users": merged_users,
        "user_turn_count": max(len(merged_users), len(turn_ids) + max(anonymous_starts.values())),
        "models": sorted(models),
        "session_id": sid,
        "cwd": cwd,
        "historical_unresolved_tool_calls": sorted(historical_pending),
    }


def find_codex_sessions(
    workspace: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    session_id: str = "",
    since: float = 0,
) -> list[dict]:
    from .workspace import native_path

    env = os.environ if env is None else env
    root = native_path(
        Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "sessions"
    )
    target = os.path.normcase(str(Path(workspace).resolve()))
    found = []
    for path in root.rglob("rollout-*.jsonl"):
        path = native_path(path)
        # Compare the ID as data, never as a glob/path supplied by the child.
        if session_id and not path.stem.endswith("-" + session_id):
            continue
        try:
            stat = path.stat()
            if stat.st_mtime < since:
                continue
            with path.open(encoding="utf-8-sig") as handle:
                rec = json.loads(handle.readline())
            if not isinstance(rec, dict) or rec.get("type") != "session_meta":
                continue
            meta = rec.get("payload") or {}
            sid, cwd = meta.get("id"), meta.get("cwd")
            if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
                continue
            if not cwd or (session_id and sid != session_id):
                continue
            if os.path.normcase(str(Path(cwd).resolve())) != target:
                continue
            found.append(
                {
                    "path": str(path),
                    "session_id": sid,
                    "mtime": str(stat.st_mtime),
                    "size": str(stat.st_size),
                }
            )
        except (OSError, ValueError, UnicodeError, AttributeError):
            continue
    return sorted(found, key=lambda item: float(item["mtime"]), reverse=True)


def find_job_sessions(job: dict, workspace: str | Path, settings: dict) -> list[dict]:
    if job_engine(job) == "codex":
        env = {**os.environ, **(settings.get("env_overrides") or {})}
        if job.get("cli_home"):
            env["CODEX_HOME"] = job["cli_home"]
        return find_codex_sessions(
            workspace, env=env
        )
    from .workspace import find_session_jsonl

    return find_session_jsonl(workspace, config_dir=job["cli_home"]) if job.get("cli_home") else find_session_jsonl(workspace)


def interruption_reason(job: dict, path: str | Path, *, side_name: str = "") -> str | None:
    if job_engine(job) == "codex":
        side = job.get("sides", {}).get(side_name, {})
        return codex_evidence(path, allow_recovered_turns=bool(side.get("completion_recovery")))["reason"]
    from .workspace import transcript_interruption_reason

    return transcript_interruption_reason(path)


def consume_codex_event(event: dict, signal: dict, emit) -> None:
    kind = event.get("type")
    if kind == "thread.started":
        signal["session_id"] = str(event.get("thread_id") or "")
    elif kind == "turn.completed":
        signal["result"] = "success"
        signal["result_is_error"] = False
        emit("[result] Codex turn.completed " + json.dumps(event.get("usage", {})))
    elif kind in ("turn.failed", "error"):
        detail = event.get("error") or event.get("message") or ""
        message = str(detail.get("message", detail) if isinstance(detail, dict) else detail)
        signal["error_message"] = message[:1000]
        # Only explicit transient transport/rate errors are safe to retry.
        permanent = re.search(
            r"\b(?:401|403)\b|insufficient_quota|billing|invalid[_ ]api[_ ]key|blocked by policy",
            message, re.I,
        )
        status = re.search(r"(?:last status:|unexpected status|HTTP)\s*(\d{3})\b", message, re.I)
        if status:
            transient = int(status.group(1)) in (408, 429, 500, 502, 503, 504)
        else:
            transient = bool(re.search(
                r"\b429\b|stream disconnected|connection reset|timed out|timeout",
                message, re.I,
            ))
        signal["retryable"] = bool(transient and not permanent)
        # error can be a recoverable reconnect notice; only turn.failed is terminal.
        if kind == "turn.failed":
            signal["result"] = "failed"
            signal["result_is_error"] = True
        emit(
            "[Codex error] "
            + str(event.get("error") or event.get("message") or "")[:1000]
        )
    elif kind in ("item.started", "item.updated", "item.completed"):
        item = event.get("item") or {}
        emit(
            f"[{kind}] {item.get('type', '')} "
            + str(
                item.get("command")
                or item.get("text")
                or item.get("message")
                or item.get("changes")
                or item.get("name")
                or ""
            )[:3000]
        )

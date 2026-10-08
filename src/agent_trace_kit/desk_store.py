"""File-backed persistence for the pair desk: settings, jobs and state recovery.

Everything lives under one home directory (default C:/AgentTraceKit-data/desk),
outside of any git repository. Writes are atomic so a crash mid-write cannot
corrupt a job record.
"""
from __future__ import annotations

import json
import time
import os
import platform
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engines import CODEX_SANDBOXES, ENGINE_LABELS, engine_id
from .baseline import normalize_source
from .cli_config import CliConnections, validate_model

DEFAULT_HOME = Path(os.environ.get("ATK_DESK_HOME", "C:/AgentTraceKit-data/desk" if os.name == "nt"
                                   else str(Path.home() / ".local/share/agenttracekit/desk")))

_LOCK = threading.RLock()

TASK_TYPES = ["0-1代码生成", "Feature迭代", "Bug修复", "代码理解", "代码重构", "工程化", "代码测试"]
DIFFICULTIES = ["困难", "地狱"]
CONCLUSIONS = ["A 更好", "Same", "B 更好"]
# Annotator-filled validity of the pair. A voided pair is kept on record but
# excluded from evaluation; the reason states whether engineering/environment caused it.
VALIDITY = ["有效", "作废-工程故障", "作废-环境未重置", "作废-其他"]
# How easily the initial environment can be reproduced by the evaluation side.
REPRO_LEVELS = ["无外部依赖", "有外部依赖，未容器化", "已容器化，可一键起环境"]

DEFAULT_SETTINGS: dict[str, Any] = {
    "claude_command": "claude",
    "codex_command": "codex",
    "default_agent": "claude",
    "codex_model": "",
    "codex_sandbox": "workspace-write",
    "codex_side_max_attempts": 3,
    # Opt-in completion mode: retain the workspace and resume the exact session.
    # Additional user turns remain visible in the evidence/checklist.
    "codex_completion_recovery": False,
    "codex_clean_single_turn": True,
    "codex_stream_max_retries": 5,
    "codex_relay_max_attempts": 5,
    "max_parallel_pairs": 2,
    "side_timeout_seconds": 1800,
    # 0 = do not kill a live Claude on silence. Deep thinking waits on the
    # gateway with near-zero CPU; killing that aborted complete turns.
    # Retry only after the process exits/errors or the side timeout fires.
    "stall_seconds": 0,
    # Upstream gateway (seed-code) cuts deep-thinking coding turns frequently;
    # every cut is retried in a fresh baseline copy until a truly complete turn
    # lands. 12 attempts covers prolonged upstream instability; each attempt can
    # take up to side_timeout_seconds, so this is a cap, not a quota to spend.
    "side_max_attempts": 12,
    # Wall-clock budget for one side across all retries (not attempts × timeout).
    "side_wall_budget_seconds": 14400,
    "activity_poll_seconds": 15,
    # claude -p permission mode. acceptEdits auto-denies ALL Bash in
    # non-interactive mode and models burn the whole turn trying workarounds;
    # side workspaces are throwaway baseline copies, so bypass is the default.
    # Overridable from the backend settings page.
    "permission_mode": "bypassPermissions",
    "oss_endpoint": "https://s3.cn-north-1.jdcloud-oss.com",
    "oss_region": "cn-north-1",
    "oss_bucket": "",
    "oss_public_base": "",
    "oss_key_prefix": "pairwise",
    "workspace_copy_excludes": "",
    "default_baseline_repo": "",
    # One-click provisioning: a new task only needs a repo name; the folder is
    # created here and an empty GitHub repo is created under the logged-in user.
    "baseline_parent_dir": str(DEFAULT_HOME.parent / "baselines"),
    "github_owner": "",          # empty == gh api user (the authenticated account)
    "github_private": False,     # match existing public baselines (evaluation side needs access)
    "default_task_type": "0-1代码生成",
    "default_difficulty": "困难",
    "default_repro_level": "无外部依赖",
    "reviewer": "",
    # Built-in screen capture of the real product run (terminal + web). The
    # operator records the genuine demo; recordings auto-stop at this cap so a
    # forgotten recording cannot run unbounded. Include time to read all output.
    "video_max_seconds": 89,
    "demo_output_chars_per_second": 40,
    "demo_command_pause_seconds": 5,
    "demo_page_pause_seconds": 6,
    "demo_final_pause_seconds": 12,
    "demo_min_seconds": 30,
    "video_fps": 15,
    "linux_auto_demo": False,
    "env_overrides": {},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_path(value: Any) -> str:
    """Normalise a pasted filesystem path: whitespace and wrapping quotes.

    Pasting from terminals/markdown often yields ``"D:\\dir"``, which Path would
    otherwise treat as a relative path and resolve against the server's cwd.
    """
    s = str(value or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


class DeskStore:
    def __init__(self, home: str | Path = DEFAULT_HOME):
        self.home = Path(home)
        self.jobs_dir = self.home / "jobs"
        self.workspaces = self.home / "workspaces"
        self.evidence = self.home / "evidence"
        self.settings_path = self.home / "settings.json"
        self.secrets_path = self.home / "secrets.env"
        self.cli_connections = CliConnections(self.home)
        for d in (self.home, self.jobs_dir, self.workspaces, self.evidence):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- settings ----------
    @staticmethod
    def _migrate_settings(data: dict[str, Any]) -> dict[str, Any]:
        if data.get("codex_clean_single_turn", True):
            data["codex_completion_recovery"] = False
        # Enforce the recording contract for old files and API updates alike.
        data["video_max_seconds"] = max(1, min(89, int(data.get("video_max_seconds", 89))))
        return data

    def settings(self) -> dict[str, Any]:
        with _LOCK:
            data = dict(DEFAULT_SETTINGS)
            if self.settings_path.exists():
                data.update(json.loads(self.settings_path.read_text(encoding="utf-8")))
            return self._migrate_settings(data)

    def save_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            current = self.settings()
            patch = dict(patch)
            if "default_agent" in patch:
                patch["default_agent"] = engine_id(patch["default_agent"])
            if "codex_sandbox" in patch and patch["codex_sandbox"] not in CODEX_SANDBOXES:
                raise ValueError("Codex sandbox 配置无效")
            if "codex_model" in patch:
                patch["codex_model"] = validate_model(patch["codex_model"])
            for path_key in ("default_baseline_repo", "baseline_parent_dir"):
                if path_key in patch:
                    patch[path_key] = clean_path(patch[path_key])
            current.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS or k in current})
            current = self._migrate_settings(current)
            self._atomic_write_json(self.settings_path, current)
            return current

    # ---------- jobs ----------
    def job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def create_job(self, data: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            job_id = "pair-" + uuid.uuid4().hex[:10]
            now = utc_now()
            settings = self.settings()
            task_type = data.get("task_type") or settings["default_task_type"]
            source = normalize_source(data, task_type)
            branch_prefix = job_id + ("-" + source["branch_suffix"] if source["branch_suffix"] else "")
            side = lambda name: {
                "side": name, "status": "pending", "workspace": "", "branch": f"{branch_prefix}-{name.lower()}",
                "initial_sha": "", "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "",
                "trace_url": "", "video_local": "", "video_url": "",
                "started_at": "", "finished_at": "", "exit_code": None, "error": "", "retry_of": "",
                # Structured per-attempt ledger (P0-1): one entry per launched
                # attempt with its archived raw stream / transcript paths, so a
                # discarded cut turn leaves auditable evidence instead of only a
                # line in the run log.
                "attempts": [],
            }
            agent = engine_id(data.get("agent") or data.get("harness") or settings.get("default_agent"))
            connection = self.cli_connections.bind(agent, data.get("cli_model") or data.get(f"{agent}_model") or "")
            if "cli_connection_id" in data and data["cli_connection_id"] != connection["id"]:
                raise ValueError("CLI 连接配置已变更，请刷新任务连接信息并重新选择模型")
            model = connection["model"] or (settings.get("codex_model", "") if agent == "codex" and not connection["id"] else "")
            connection["model"] = validate_model(model)
            job = {
                "id": job_id,
                "name": data.get("name") or source.get("github_repo") or str(data.get("github_repo", "")).strip() or job_id,
                "prompt": data["prompt"],
                "task_type": task_type,
                "difficulty": data.get("difficulty") or settings["default_difficulty"],
                "stack": data.get("stack", ""),
                "repro_level": data.get("repro_level") or settings["default_repro_level"],
                "env_desc": data.get("env_desc", ""),
                "check_commands": data.get("check_commands", ""),
                "copy_excludes": data.get("copy_excludes", ""),
                "baseline_repo": clean_path(data.get("baseline_repo", "")),
                # One-click provisioning fields. github_repo drives auto-create;
                # github_readme is the initial blurb; baseline_parent_dir/github_owner/
                # github_private fall back to settings when blank.
                "github_repo": str(data.get("github_repo", "")).strip(),
                "github_readme": str(data.get("github_readme", "")).strip(),
                "github_private": bool(data.get("github_private", settings.get("github_private", False))),
                "github_owner": str(data.get("github_owner", "") or settings.get("github_owner", "")).strip(),
                "baseline_parent_dir": clean_path(
                    data.get("baseline_parent_dir", "") or settings.get("baseline_parent_dir", "")
                ),
                "baseline_sha": "",
                "baseline_url": "",
                "agent": agent,
                "harness": ENGINE_LABELS[agent],
                "codex_model": model if agent == "codex" else "",
                "claude_model": model if agent == "claude" else "",
                "cli_connection": connection,
                "cli_home": str((self.home / "cli-runtime" / agent / job_id).resolve()),
                "execution_policy": "2026-10-04-single-agent-v1",
                "harness_version": "",
                "os_name": platform.system(),
                "status": "draft",
                "created_at": now,
                "updated_at": now,
                "sides": {"A": side("A"), "B": side("B")},
                "review": {
                    "validity": "", "conclusion": "", "reason": "",
                    "a_delivery_score": "", "a_delivery_description": "",
                    "b_delivery_score": "", "b_delivery_description": "",
                    "reviewer": settings.get("reviewer", ""), "note": "",
                    # Human-only gate (P1-4): once the reviewer commits a
                    # judgement with the no-AI attestation, run/retry actions are
                    # refused server-side until an explicit unlock.
                    "ai_confirmed": False, "ai_confirmed_at": "", "locked_at": "",
                    "auto_review": {}, "qc_status": "", "qc_feedback": "",
                },
                "uploads": {"A": {}, "B": {}},
                "exported": False,
                "delivery_quality_required": True,
                # Visibility layer, independent of run/review state. An archived
                # job is hidden from the default queue and never auto-resumed;
                # its record/evidence are untouched and it can be restored.
                "archived": False,
                "archived_at": "",
            }
            job.update(source)
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def get_job(self, job_id: str) -> dict[str, Any]:
        with _LOCK:
            return json.loads(self.job_path(job_id).read_text(encoding="utf-8"))

    def list_jobs(self) -> list[dict[str, Any]]:
        with _LOCK:
            jobs = []
            for p in self.jobs_dir.glob("*.json"):
                try:
                    jobs.append(json.loads(p.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    continue
            jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
            return jobs

    def delete_job(self, job_id: str) -> None:
        """Remove the job record (workspaces/evidence are removed by the caller)."""
        with _LOCK:
            path = self.job_path(job_id)
            if path.exists():
                path.unlink()

    def update_job(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            job = self.get_job(job_id)
            for key, value in patch.items():
                if key in ("id",):
                    continue
                job[key] = value
            job["updated_at"] = utc_now()
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def update_side(self, job_id: str, side_name: str, patch: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            job = self.get_job(job_id)
            side = job["sides"][side_name]
            side.update(patch)
            job["updated_at"] = utc_now()
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def update_review(self, job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            job = self.get_job(job_id)
            review = job["review"]
            for key in ("validity", "conclusion", "reason", "reviewer", "note",
                        "a_delivery_score", "a_delivery_description",
                        "b_delivery_score", "b_delivery_description",
                        "ai_confirmed", "ai_confirmed_at", "locked_at",
                        "qc_status", "qc_feedback", "evidence_binding"):
                if key in patch:
                    review[key] = patch[key]
            job["updated_at"] = utc_now()
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def begin_manual_retry(self, job_id: str, side_name: str, attempt_budget_start: int) -> dict[str, Any]:
        """Archive the current result and atomically reserve a fresh allowance."""
        with _LOCK:
            job = self.get_job(job_id)
            if job.get("archived") or self.review_locked(job):
                raise RuntimeError("任务已归档或评审已锁定，请先恢复或解锁再重跑")
            side = job["sides"][side_name]
            if side["status"] in ("preparing", "running", "collecting"):
                raise RuntimeError("该侧正在运行或准备，不能重复重跑")
            archive = self.evidence_dir(job_id) / "manual-reruns"
            archive.mkdir(parents=True, exist_ok=True)
            self._atomic_write_json(archive / f"{side_name.lower()}-{time.time_ns()}.json", {
                "job": job_id, "side": side_name, "requested_at": utc_now(),
                "previous_side": side, "previous_review": job.get("review", {}),
                "previous_uploads": job.get("uploads", {}), "previous_exported": job.get("exported", False),
                "attempt_budget_start": attempt_budget_start,
            })
            if (side.get("failure_capture") or {}).get("origin") == "posthoc-failed-worktree":
                # Frozen failure evidence stays immutable at its recorded path.
                # Retry the original canonical worktree, never the snapshot clone.
                side["workspace"] = str(self.workspace_pair_dir(job_id) / side_name.lower())
            side.update({
                "status": "preparing", "error": "", "attempt_budget_start": attempt_budget_start,
                "clean_deadline_epoch": None, "completion_deadline_epoch": None,
                "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "",
                "trace_url": "", "pushed": False, "exit_code": None,
                "video_local": "", "video_url": "", "demo": {}, "failure_capture": {}, "check_results": [],
                "started_at": "", "finished_at": "", "duration_seconds": 0,
            })
            review = job.get("review", {})
            for key in review:
                if key != "reviewer":
                    review[key] = False if key == "ai_confirmed" else {} if key == "auto_review" else ""
            job.setdefault("uploads", {})[side_name] = {}
            job["exported"] = False
            job["updated_at"] = utc_now()
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def update_failed_capture(self, job_id: str, side_name: str, patch: dict[str, Any],
                              expected_sides: dict[str, Any],
                              expected_review: dict[str, Any]) -> dict[str, Any]:
        """Bind a posthoc snapshot without changing the failed generation result."""
        allowed = {"workspace", "head_sha", "head_url", "pushed", "session_id",
                   "jsonl_local", "failure_capture"}
        if side_name not in ("A", "B") or set(patch) != allowed:
            raise RuntimeError("失败采集只能更新指定的证据字段")
        capture = patch.get("failure_capture")
        if (not isinstance(capture, dict) or capture.get("origin") != "posthoc-failed-worktree"
                or capture.get("completed") is not False):
            raise RuntimeError("失败采集必须保留未完成轮次的来源说明")
        with _LOCK:
            job = self.get_job(job_id)
            if (job.get("archived") or job.get("exported")
                    or job.get("review") != expected_review
                    or job.get("sides") != expected_sides):
                raise RuntimeError("采集期间任务或用户评价已变化，拒绝覆盖")
            if (job["sides"][side_name].get("status") != "failed"
                    or any(s.get("status") not in {"done", "failed"} for s in job["sides"].values())):
                raise RuntimeError("失败采集必须等待本题两侧执行终了")
            if any(expected_review.get(k) for k in ("validity", "conclusion", "reason", "note",
                    "a_delivery_score", "a_delivery_description", "b_delivery_score",
                    "b_delivery_description", "auto_review", "locked_at", "ai_confirmed",
                    "ai_confirmed_at", "qc_status", "qc_feedback")):
                raise RuntimeError("已有评价的任务不能自动重新采集")
            job["sides"][side_name].update(patch)
            job["updated_at"] = utc_now()
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def update_authorized_review(self, job_id: str, patch: dict[str, Any],
                                 expected_sides: dict[str, Any]) -> dict[str, Any]:
        """Save an AI-assisted draft only while its exact evidence is current.

        User validity and the human-only no-AI lock are never writable here.
        Checking and writing under the store lock prevents a concurrent side
        restart or user decision from being silently overwritten.
        """
        allowed = {"conclusion", "reason", "a_delivery_score", "a_delivery_description",
                   "b_delivery_score", "b_delivery_description", "auto_review",
                   "qc_status", "qc_feedback", "note"}
        if set(patch) - allowed:
            raise RuntimeError("自动评价不能写入有效性或人工确认字段")
        with _LOCK:
            job = self.get_job(job_id)
            review = job["review"]
            if job.get("archived") or job.get("exported") or review.get("locked_at") or review.get("validity"):
                raise RuntimeError("已有用户判断、已导出或归档的任务不能自动覆盖评价")
            for name in ("A", "B"):
                current = job["sides"][name]
                for key in ("status", "head_sha", "session_id", "finished_at", "demo",
                            "jsonl_local", "trace_url", "video_local", "video_url"):
                    if current.get(key) != expected_sides[name].get(key):
                        raise RuntimeError(f"{name} 侧证据已变化，必须重新评价")
            review.update(patch)
            job["updated_at"] = utc_now()
            self._atomic_write_json(self.job_path(job_id), job)
            return job

    def set_archived(self, job_id: str, archived: bool) -> dict[str, Any]:
        """Toggle the archive visibility flag without touching run/review data."""
        patch = {"archived": bool(archived),
                 "archived_at": utc_now() if archived else ""}
        return self.update_job(job_id, patch)

    def evidence_dir(self, job_id: str) -> Path:
        path = self.evidence / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def workspace_pair_dir(self, job_id: str) -> Path:
        path = self.workspaces / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def review_locked(job: dict[str, Any]) -> bool:
        """True once a human judgement was committed with the no-AI attestation.

        A locked pair refuses run/retry/prepare server-side; evidence and the GSB
        text stay editable until an explicit unlock.
        """
        return bool(job.get("review", {}).get("locked_at"))

    # ---------- internals ----------
    @staticmethod
    def _atomic_write_json(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(value, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

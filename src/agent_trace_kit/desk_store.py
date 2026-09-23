"""File-backed persistence for the pair desk: settings, jobs and state recovery.

Everything lives under one home directory (default C:/AgentTraceKit-data/desk),
outside of any git repository. Writes are atomic so a crash mid-write cannot
corrupt a job record.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path(os.environ.get("ATK_DESK_HOME", "C:/AgentTraceKit-data/desk"))

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
    "default_baseline_repo": r"D:\myprojects\GoletaLab数据标注\github-base\logistic-irls",
    # One-click provisioning: a new task only needs a repo name; the folder is
    # created here and an empty GitHub repo is created under the logged-in user.
    "baseline_parent_dir": r"D:\myprojects\GoletaLab数据标注\github-base",
    "github_owner": "",          # empty == gh api user (the authenticated account)
    "github_private": False,     # match existing public baselines (evaluation side needs access)
    "default_task_type": "0-1代码生成",
    "default_difficulty": "困难",
    "default_repro_level": "无外部依赖",
    "reviewer": "徐子扬",
    # Built-in screen capture of the real product run (terminal + web). The
    # operator records the genuine demo; recordings auto-stop at this cap so a
    # forgotten recording cannot run unbounded. 89 rather than 90 to stay just
    # under a strict 90s boundary check downstream. A few seconds is acceptable.
    "video_max_seconds": 89,
    "video_fps": 15,
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
        for d in (self.home, self.jobs_dir, self.workspaces, self.evidence):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- settings ----------
    @staticmethod
    def _migrate_settings(data: dict[str, Any]) -> dict[str, Any]:
        # The old default cap was exactly 90s; it sits on a strict downstream
        # boundary. Move only that legacy default (not an operator-chosen value)
        # to 89 without rewriting the persisted file.
        if data.get("video_max_seconds") == 90:
            data["video_max_seconds"] = 89
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
            for path_key in ("default_baseline_repo", "baseline_parent_dir"):
                if path_key in patch:
                    patch[path_key] = clean_path(patch[path_key])
            current.update({k: v for k, v in patch.items() if k in DEFAULT_SETTINGS or k in current})
            self._atomic_write_json(self.settings_path, current)
            return current

    # ---------- jobs ----------
    def job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def create_job(self, data: dict[str, Any]) -> dict[str, Any]:
        with _LOCK:
            job_id = "pair-" + uuid.uuid4().hex[:10]
            now = utc_now()
            side = lambda name: {
                "side": name, "status": "pending", "workspace": "", "branch": f"{job_id}-{name.lower()}",
                "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "",
                "trace_url": "", "video_local": "", "video_url": "",
                "started_at": "", "finished_at": "", "exit_code": None, "error": "", "retry_of": "",
                # Structured per-attempt ledger (P0-1): one entry per launched
                # attempt with its archived raw stream / transcript paths, so a
                # discarded cut turn leaves auditable evidence instead of only a
                # line in the run log.
                "attempts": [],
            }
            settings = self.settings()
            job = {
                "id": job_id,
                "name": data.get("name") or str(data.get("github_repo", "")).strip() or job_id,
                "prompt": data["prompt"],
                "task_type": data.get("task_type") or settings["default_task_type"],
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
                "harness": "Claude Code",
                "harness_version": "",
                "os_name": "Windows",
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
                        "ai_confirmed", "ai_confirmed_at", "locked_at"):
                if key in patch:
                    review[key] = patch[key]
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

"""Background execution engine for A/B pairs.

- one queue, bounded parallelism (max parallel pairs + both sides concurrent)
- default evaluation runs use a frozen prompt and clean baseline retries
- each pair records its CLI; model configuration is inherited unless an
  explicit Codex model was selected; this engine never sets ANTHROPIC_MODEL
- opt-in completion recovery keeps work, resumes the bound Codex session and
  preserves attempt counts/deadlines across restarts

Upstream gateway cuts are recorded as incomplete attempts, never as completed
model results. Recovery mode records extra continuation turns explicitly;
default mode retains its original fresh-baseline policy.
"""
from __future__ import annotations

import io
import json
import hashlib
import os
import queue
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from . import ghutil
from . import jobobj
from . import procmon
from . import workspace as ws
from . import engines
from . import baseline as baseline_mod
from .desk_store import DeskStore

# Minimum attempts even if settings.json carries an older, smaller value:
# upstream instability must never silently cap retries at a stale number.
MIN_MAX_ATTEMPTS = 12
# Cap a single retry wait so a side stays responsive to manual abort.
MAX_BACKOFF_SECONDS = 120
# Hard ceiling so 16 attempts × a huge side_timeout cannot run a side for days.
DEFAULT_SIDE_WALL_SECONDS = 4 * 3600
MAX_SIDE_WALL_SECONDS = 8 * 3600


MAX_PARALLEL_PAIRS = 16
MAX_SIDE_ATTEMPTS = 16

COMPLETION_PROMPT = (
    "这是运行框架在接口中断或输出达到上限后的自动续接。请继续本会话原始任务，"
    "保留并先检查当前工作区已有进度，不要从头重建。将剩余实现拆成小段及时写入文件，"
    "逐步运行实际测试、修复失败并完成原要求的演示配置。避免长篇重复规划；"
    "仅在实际完成后报告结果，明确保留未完成或失败的事实。"
)


def completion_retryable(result: dict) -> bool:
    """Only observed transient failures/output exhaustion permit continuation."""
    if result.get("aborted") or result.get("completed"):
        return False
    message = str(result.get("failure") or "")
    if re.search(r"insufficient_quota|unauthori[sz]ed|forbidden|blocked by policy|invalid_api_key", message, re.I):
        return False
    status = re.search(r"(?:HTTP|unexpected status|last status:)\s*(\d{3})\b", message, re.I)
    if status and int(status.group(1)) not in (408, 429, 500, 502, 503, 504):
        return False
    return bool(result.get("retryable") or re.search(
        r"stream disconnected|connection reset|timed out|timeout|total-timeout|"
        r"max_output_tokens|HTTP\s*(?:408|429|500|502|503|504)|单侧总超时", message, re.I))


def watchdog_idle_seconds(
    idle_for: float,
    *,
    cpu_io_busy: bool,
    prev_transcript_mtime: float,
    cur_transcript_mtime: float,
    poll_every: float,
) -> tuple[float, float]:
    """Advance idle time only when CPU/IO and the transcript are both frozen.

    Activity is a *new* transcript write since the last poll, not "newer than
    launch". Comparing to the launch baseline made a single early write keep
    the side "busy" forever after the stream died.
    """
    grew = cur_transcript_mtime > prev_transcript_mtime
    last = max(prev_transcript_mtime, cur_transcript_mtime)
    if cpu_io_busy or grew:
        return 0.0, last
    return idle_for + poll_every, last


def stall_seconds_from_settings(settings: dict) -> int:
    """0 = do not kill on silence; wait for the Claude process to exit or error."""
    try:
        raw = int(settings.get("stall_seconds", 0) or 0)
    except (TypeError, ValueError):
        raw = 0
    return max(0, raw)


def side_wall_seconds(settings: dict) -> int:
    try:
        raw = int(settings.get("side_wall_budget_seconds", DEFAULT_SIDE_WALL_SECONDS)
                  or DEFAULT_SIDE_WALL_SECONDS)
    except (TypeError, ValueError):
        raw = DEFAULT_SIDE_WALL_SECONDS
    return max(60, min(MAX_SIDE_WALL_SECONDS, raw))

# python -m <mod> that is expected to exit (tests/linters/installers).
_FINITE_PY_MODULES = {
    "unittest", "pytest", "pip", "ensurepip", "py_compile", "compileall",
    "doctest", "ruff", "mypy", "black", "flake8", "pylint", "isort", "tests",
    "venv", "coverage", "tox", "nox", "build", "json",
}
_START_SUBSTRINGS = (
    "npm start", "npm run start", "npm run dev", "npm run serve", "npm run preview",
    "yarn start", "yarn dev", "pnpm start", "pnpm dev",
    "npx serve", "npx vite", "flask run",
)
_START_BINARIES = frozenset({"uvicorn", "gunicorn", "daphne", "hypercorn"})
# Interpreter flags that may sit between `python` and the script / -m module
# (`python -u src/server.py`, `python -u -m http.server`).
_PY_INTERP = r"(?:pythonw?(?:\d+(?:\.\d+)?)?|py)(?:\.exe)?"
_PY_FLAGS = r"(?:\s+(?:-\d+(?:\.\d+)?|-[uBEsvI]|-X\S*))*"
_PY_SCRIPT_START = re.compile(
    rf"^{_PY_INTERP}{_PY_FLAGS}\s+"
    r"(?:\S+[/\\])?(?:app|main|ui|server|manage|serve)\.py\b"
)
_PY_MODULE = re.compile(
    rf"^{_PY_INTERP}{_PY_FLAGS}\s+-m\s+([\w.]+)"
)
_NODE_SERVER = re.compile(r"^node(?:\.exe)?\s+(?:server(?:\.js)?|src[/\\]server\.js|\.)\b")
_PHP_SERVER = re.compile(r"php(?:\.\w+)?\s+-S\b")


def is_long_running_start(command: str) -> bool:
    """True for GUI/dev-server commands that will not exit on their own.

    Running those as blocking checks on Windows deadlocks: timeout kills only
    ``cmd.exe``, the child keeps the inherited stdout pipe open, and
    ``communicate()`` waits forever — leaving the side stuck on 运行中.
    """
    raw = command.strip()
    if not raw:
        return False
    # php -S is the built-in server (case-sensitive). php -s is syntax highlight.
    if any(_PHP_SERVER.search(part) for part in re.split(r"\s*(?:&&|\|\||;)\s*", raw) if part):
        return True
    s = " ".join(raw.lower().split())
    return any(_segment_is_start(part) for part in re.split(r"\s*(?:&&|\|\||;)\s*", s) if part)


def _segment_is_start(s: str) -> bool:
    if any(tok in s for tok in _START_SUBSTRINGS):
        return True
    first = s.split()[0] if s else ""
    if first in _START_BINARIES:
        return True
    if re.match(r"go\s+run\b", s):
        return True
    if re.match(r"dotnet\s+run\b", s):
        return True
    if re.match(r"cargo\s+run\b", s):
        tokens = s.split()
        if "--help" in tokens or "-h" in tokens or "--version" in tokens:
            return False
        return True
    if _PY_SCRIPT_START.match(s) or _NODE_SERVER.match(s):
        return True
    m = _PY_MODULE.match(s)
    if m:
        root = m.group(1).split(".")[0]
        if root in _FINITE_PY_MODULES:
            return False
        if re.search(r"(^|\s)--(selftest|help|version|check)\b", s):
            return False
        return True
    return False


def run_check_commands(
    commands: list[str],
    cwd: str | Path,
    timeout: int = 600,
    *,
    kill_job_factory: Callable[[], "jobobj.KillJob"] | None = None,
    should_abort: Callable[[], bool] | None = None,
) -> list[dict]:
    """Run finite checks; skip launchers; timeout reaps the whole process tree.

    stdout goes to a temp file (not a pipe) so a surviving grandchild cannot
    block the parent thread after the shell is killed. ``should_abort`` is
    polled so a UI 中止 cannot leave the side stuck in a 600s wait.
    """
    results: list[dict] = []
    factory = kill_job_factory or jobobj.KillJob
    for command in commands:
        if should_abort and should_abort():
            results.append({"command": command, "exit_code": None, "aborted": True})
            break
        if is_long_running_start(command):
            results.append({
                "command": command,
                "exit_code": None,
                "skipped": True,
                "reason": "启动类命令（GUI/开发服务）不会自行退出，已跳过以免卡住收尾",
            })
            continue
        results.append(_run_one_check(
            command, cwd, timeout, factory, should_abort=should_abort))
    return results


def _wait_proc(proc: subprocess.Popen, timeout: float = 30) -> None:
    """Wait for a child; never let TimeoutExpired escape to the caller."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _feed_stdin(proc: subprocess.Popen, data: str, timeout: float = 30) -> None:
    """Write the prompt and close stdin. Close is what delivers EOF to ``claude -p``.

    Runs on a helper thread so a stuck child that never reads cannot freeze the
    attempt thread (classic stdin-full / stdout-full deadlock). On join
    timeout we terminate the child — never ``stdin.close()`` from this thread,
    which deadlocks on CPython's IO lock (and ``os.close(fd)`` can also block
    on Windows while the writer is in ``write()``).
    """
    if proc.stdin is None:
        return

    def _write() -> None:
        try:
            if isinstance(proc.stdin, io.TextIOWrapper):
                proc.stdin.reconfigure(newline="")
            proc.stdin.write(data)
            proc.stdin.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    t = threading.Thread(target=_write, daemon=True, name="atk-stdin")
    t.start()
    t.join(timeout)
    if t.is_alive():
        try:
            proc.terminate()
        except OSError:
            pass
        t.join(5)


def _reap_check(proc: subprocess.Popen, job: "jobobj.KillJob", assigned: bool) -> None:
    if assigned:
        job.terminate()
    if proc.pid and (os.name != "nt" or proc.poll() is None):
        procmon.kill_tree(proc.pid)
    _wait_proc(proc, timeout=15)


def _run_one_check(
    command: str,
    cwd: str | Path,
    timeout: int,
    kill_job_factory: Callable[[], "jobobj.KillJob"],
    *,
    should_abort: Callable[[], bool] | None = None,
) -> dict:
    job = kill_job_factory()
    out_path: Path | None = None
    proc: subprocess.Popen | None = None
    try:
        fd, name = tempfile.mkstemp(prefix="atk-check-", suffix=".log")
        os.close(fd)
        out_path = Path(name)
        with out_path.open("wb") as out:
            proc = subprocess.Popen(
                command, cwd=str(cwd), shell=True,
                stdout=out, stderr=subprocess.STDOUT,
                **procmon.hidden_console_kwargs(),
            )
            assigned = bool(job.alive and job.add_pid(proc.pid))
            deadline = time.time() + timeout
            while True:
                if should_abort and should_abort():
                    _reap_check(proc, job, assigned)
                    return {"command": command, "exit_code": None, "aborted": True}
                remaining = deadline - time.time()
                if remaining <= 0:
                    _reap_check(proc, job, assigned)
                    return {"command": command, "exit_code": 124}
                try:
                    proc.wait(timeout=min(1.0, remaining))
                    return {"command": command, "exit_code": proc.returncode}
                except subprocess.TimeoutExpired:
                    continue
    except OSError as exc:
        return {"command": command, "exit_code": 127, "error": str(exc)}
    finally:
        job.close()
        if out_path is not None:
            try:
                out_path.unlink()
            except OSError:
                pass


def retry_backoff_seconds(attempt: int, rng: random.Random | None = None) -> int:
    """Exponential backoff with jitter before retry ``attempt`` (attempt >= 2).

    Bases: 8s, 16s, 32s, 64s, then capped at 120s, each with ±25% jitter so
    the two sides of a pair do not hammer the gateway in lock-step.
    """
    r = rng or random
    base = min(MAX_BACKOFF_SECONDS, 8 * (2 ** min(attempt - 2, 4)))
    jitter = int(base * 0.25 * (r.random() * 2 - 1))
    return max(0, base + jitter)


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def codex_retry_backoff_seconds(attempt: int, rng: random.Random) -> int:
    """60, 120, 240, ... seconds plus positive jitter, capped at 15 minutes."""
    base = min(720, 60 * 2 ** min(max(0, attempt - 2), 4))
    return base + int(base * 0.25 * rng.random())


def _managed_codex(job: dict) -> bool:
    return engines.job_engine(job) == "codex" and (job.get("cli_connection") or {}).get("mode") == "project"


def claude_version(command: str = "claude") -> str:
    try:
        from .procmon import run_hidden
        p = run_hidden([command, "--version"], capture_output=True, text=True, timeout=15)
        return p.stdout.strip() if p.returncode == 0 else ""
    except OSError:
        return ""


class PairRunner:
    def __init__(self, store: DeskStore):
        self.before_pair = None  # Optional host resource gate, supplied by DeskServer.
        self.store = store
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._booted = False
        self._active: dict[str, bool] = {}
        self._queued: set[str] = set()
        self._procs: dict[str, subprocess.Popen] = {}
        self._inflight: set[str] = set()
        self._abort: set[str] = set()
        self._lock = threading.RLock()

    @staticmethod
    def _new_kill_job() -> "jobobj.KillJob":
        """Seam for the per-attempt kill-on-close Job Object (tests override it)."""
        return jobobj.KillJob()

    # ---------- lifecycle ----------
    def _reg_path(self, job_id: str, side_name: str) -> Path:
        d = self.store.home / "running"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{job_id}__{side_name}.json"

    def is_side_live(self, job_id: str, side_name: str) -> bool:
        """True only when a worker thread or CLI process is actually up.

        ``preparing`` (baseline recopy) is busy but not live: the follow view
        must not keep saying 准备中 after Claude has already started writing.
        """
        key = f"{job_id}/{side_name}"
        with self._lock:
            if key in self._inflight:
                return True
            proc = self._procs.get(key)
        return proc is not None and proc.poll() is None

    def is_side_running(self, job_id: str, side_name: str) -> bool:
        """True while the side is busy: in-flight thread, live CLI, or preparing.

        Recopy/backoff used to look like 待运行 in the UI while retry_side
        still refused, because only the live Popen was checked.
        """
        if self.is_side_live(job_id, side_name):
            return True
        try:
            status = self.store.get_job(job_id)["sides"][side_name]["status"]
        except (OSError, KeyError, json.JSONDecodeError):
            return False
        return status in ("running", "preparing", "collecting")

    def recover(self) -> dict:
        """Reap orphaned runs after a restart, then mark unfinished work retryable."""
        from . import procmon
        recovered: list[str] = []
        killed: list[str] = []
        registered: set[str] = set()
        running_dir = self.store.home / "running"
        if running_dir.is_dir():
            for reg in running_dir.glob("*.json"):
                try:
                    info = json.loads(reg.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    info = {}
                registered.add(f"{info.get('job_id', '?')}/{info.get('side', '?')}")
                pid = int(info.get("pid") or 0)
                if os.name != "nt" and pid:
                    current = procmon.snapshot().get(pid)
                    if not current or not info.get("process_start") or current.get("start_time") != info["process_start"]:
                        pid = 0  # Do not kill an unrelated process after PID reuse/reboot.
                if pid:
                    try:
                        procmon.kill_tree(pid)
                        killed.append(f"{info.get('job_id','?')}/{info.get('side','?')}")
                    except Exception:
                        pass
                try:
                    reg.unlink()
                except OSError:
                    pass
        if killed:
            time.sleep(1.5)
        resume: list[tuple[str, str]] = []
        for job in self.store.list_jobs():
            changed = False
            for side_name, side in job["sides"].items():
                # A pending side normally means "prepared, not enqueued yet";
                # but a live registry left for it means the crash happened in
                # the tiny window after fresh-copy and before status=running.
                in_flight = side["status"] in ("preparing", "running", "collecting") or (
                    side["status"] == "pending" and f"{job['id']}/{side_name}" in registered)
                if in_flight:
                    can_resume = bool(job.get("baseline_sha")) and not self.store.review_locked(job)
                    note = (
                        "交付台重启，已终止上次未完成的运行"
                        + (("，将保留工作区续接" if _managed_codex(job) and self.store.settings().get("codex_completion_recovery")
                            else "，将自动从基线重跑") if can_resume else "；点「重跑该侧」从基线重新执行")
                        + ("（旧进程已清理）" if any(x.startswith(f"{job['id']}/{side_name}") for x in killed) else "")
                    )
                    self.store.update_side(job["id"], side_name, {
                        "status": "pending" if can_resume else "failed",
                        "error": note,
                        "finished_at": _stamp(),
                    })
                    changed = True
                    recovered.append(f"{job['id']}/{side_name}")
                    if can_resume:
                        resume.append((job["id"], side_name))
            if job["status"] == "running":
                self.store.update_job(job["id"], {"status": "ready" if changed else job["status"]})
        for job_id, side_name in resume:
            try:
                self.retry_side(job_id, side_name, enqueue=False, renew_budget=False)
            except Exception as exc:
                self.store.update_side(job_id, side_name, {
                    "status": "failed",
                    "error": f"交付台重启后自动重跑失败：{exc}",
                    "finished_at": _stamp(),
                })
        return {"recovered": recovered, "orphans_killed": killed}

    def start(self) -> None:
        if not self._booted:
            self.recover()
            self._stop.clear()
            self._booted = True
            self.ensure_workers()
            # In-memory queue dies with the process; pending/failed sides on disk
            # would otherwise sit at 待运行 until someone clicks 开始/重试.
            self._resume_incomplete_jobs()
            return
        self.ensure_workers()

    def ensure_workers(self) -> int:
        """Grow the worker pool; admission also enforces the current pair limit.

        The pool is created once at desk boot from whatever the setting was
        *then*; saving 8 later used to do nothing until restart. Extra idle
        workers remain idle after a reduction. Existing pairs drain naturally;
        the admission check prevents those workers from exceeding the new cap.
        """
        wanted = max(1, min(MAX_PARALLEL_PAIRS, int(
            self.store.settings().get("max_parallel_pairs", 2) or 2)))
        self._stop.clear()
        while len(self._workers) < wanted:
            i = len(self._workers)
            t = threading.Thread(target=self._worker_loop, name=f"pair-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)
        return wanted

    def _resume_incomplete_jobs(self) -> None:
        for job in self.store.list_jobs():
            if self.store.review_locked(job) or job.get("status") == "evidence_ready":
                continue
            if not job.get("baseline_sha"):
                continue
            # pending = prepared; preparing = recover just recopied an interrupted side.
            if any(job["sides"][s]["status"] in ("pending", "preparing") for s in ("A", "B")):
                self.enqueue(job["id"])

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            if self._active.get(job_id) or job_id in self._queued:
                return
            # Archived jobs are parked by the operator and must never be
            # auto-resumed on restart; restore them from the queue view first.
            try:
                if self.store.get_job(job_id).get("archived"):
                    return
            except (OSError, KeyError):
                return
            self._queued.add(job_id)
        self._queue.put(job_id)

    def active_jobs(self) -> list[str]:
        with self._lock:
            return [jid for jid, on in self._active.items() if on]

    # ---------- worker ----------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = None
            # Reserve capacity and dequeue under the same lock. Pool size alone
            # cannot enforce a reduced limit while old workers still exist.
            with self._lock:
                if not self._queue.empty():
                    limit = max(1, min(MAX_PARALLEL_PAIRS, int(
                        self.store.settings().get("max_parallel_pairs", 2) or 2)))
                    if sum(self._active.values()) < limit:
                        try:
                            job_id = self._queue.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            self._active[job_id] = True
            if job_id is None:
                self._stop.wait(0.25)
                continue
            try:
                self._run_job(job_id)
            except Exception as exc:  # never kill the worker thread
                try:
                    self.store.update_job(job_id, {"status": "failed", "error": f"引擎异常: {exc}"})
                except Exception:
                    pass
            finally:
                with self._lock:
                    self._active[job_id] = False
                    self._queued.discard(job_id)
                self._queue.task_done()
                # retry_side of a sibling while this pair was _active only
                # recopied; pick that pending/preparing side up now.
                try:
                    job = self.store.get_job(job_id)
                except Exception:
                    job = None
                if (job and not self.store.review_locked(job) and job.get("status") != "evidence_ready"
                        and job.get("baseline_prepared") is not False):
                    if any(job["sides"][s]["status"] in ("pending", "preparing") for s in ("A", "B")):
                        self.enqueue(job_id)

    def _run_job(self, job_id: str) -> None:
        while self.before_pair and not self.before_pair():
            if self._stop.wait(1):
                return
        job = self.store.get_job(job_id)
        # Re-check at dequeue time: the job may have been archived while it was
        # sitting in the in-memory queue. Archived jobs never start a run.
        if job.get("archived"):
            return
        if not job.get("baseline_sha"):
            raise RuntimeError("任务尚未准备（缺少基线快照）")
        # Skip sides that already finished (e.g. single-side retry); skip whole done jobs.
        if job.get("status") == "evidence_ready":
            return
        if job.get("baseline_prepared") is False:
            # A clone/submodule/side checkout may have failed after the SHA was
            # frozen. Resume preparation before launching either CLI.
            self.prepare(job_id)
        self.store.update_job(job_id, {"status": "running", "error": ""})
        # Re-read after the status flip: retry_side may have just copied a
        # pending workspace, and a stale snapshot would skip the side to run.
        job = self.store.get_job(job_id)
        threads = []
        for side_name in ("A", "B"):
            status = job["sides"][side_name]["status"]
            # failed still needs retry_side's recopy; launching it here would
            # start Claude on the dirty tree, then the sibling recopy would
            # rmtree underneath and leave the UI stuck on 准备中.
            if status in ("done", "failed"):
                continue
            if self.is_side_live(job_id, side_name):
                continue
            t = threading.Thread(target=self._run_side_safe, args=(job_id, side_name), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        self._sync_job_status(job_id)

    def _sync_job_status(self, job_id: str) -> None:
        """Derive the job-level status from the two sides' statuses.

        A single-side retry while the other side is still running must not
        downgrade the job tag to 待运行: pending/failed sides only win when
        nothing is running.
        """
        job = self.store.get_job(job_id)
        statuses = [job["sides"][s]["status"] for s in ("A", "B")]
        busy = any(v in ("running", "preparing", "collecting") for v in statuses) or any(
            self.is_side_running(job_id, s) for s in ("A", "B")
        )
        if all(v == "done" for v in statuses):
            new_status = "evidence_ready"
        elif busy:
            new_status = "running"
        elif any(v == "failed" for v in statuses):
            new_status = "failed"
        else:
            new_status = "ready"
        if job.get("status") != new_status:
            self.store.update_job(job_id, {"status": new_status})

    def _run_side_safe(self, job_id: str, side_name: str) -> None:
        key = f"{job_id}/{side_name}"
        with self._lock:
            self._inflight.add(key)
        try:
            self.run_side(job_id, side_name)
        except Exception as exc:
            self.store.update_side(job_id, side_name, {
                "status": "failed", "error": str(exc), "finished_at": _stamp(),
            })
        finally:
            with self._lock:
                self._inflight.discard(key)

    # ---------- per-side pipeline ----------
    def _provision_baseline(self, job: dict) -> dict:
        """Create the GitHub repo + local baseline folder when no baseline exists yet.

        Triggered when a job carries a ``github_repo`` name but no usable local
        baseline directory. Reuses an existing remote repo if it already exists;
        never deletes or force-pushes anything.
        """
        name = (job.get("github_repo") or "").strip()
        if not name:
            raise RuntimeError("基线仓库目录无效，且未提供 GitHub 仓库名，无法自动创建")
        parent = (job.get("baseline_parent_dir")
                  or self.store.settings().get("baseline_parent_dir", ""))
        if not parent:
            raise RuntimeError("未配置基线父目录（后台设置里的「基线父目录」）")
        repo, created = ghutil.ensure_remote_repo(
            name,
            job.get("github_readme", ""),
            private=bool(job.get("github_private", False)),
            owner=job.get("github_owner", ""),
        )
        info = ws.provision_baseline_folder(
            parent, repo.name, repo.url, repo.default_branch, job.get("github_readme", ""),
        )
        self.store.update_job(job["id"], {
            "baseline_repo": info["path"],
            "github_repo": repo.full_name,
            "github_created": created,
            "github_url": repo.url,
            "github_visibility": repo.visibility.lower(),
        })
        return {**info, "github": repo.__dict__, "created": created}

    def prepare(self, job_id: str) -> dict:
        """Prepare a baseline and A/B workspaces, retaining a frozen imported SHA."""
        job = self.store.get_job(job_id)
        if any(self.is_side_live(job_id, s) for s in ("A", "B")):
            raise RuntimeError("任务正在运行，不能重新准备基线")
        baseline = job.get("baseline_repo", "")
        provisioned: dict | None = None
        if job.get("source_mode") == "github_commit":
            snap = baseline_mod.prepare_commit_baseline(
                job["source_repo"], job.get("baseline_sha") or job["source_commit"],
                self.store.workspace_pair_dir(job_id) / "baseline",
            )
            baseline = snap["path"]
        else:
            if job.get("source_mode") == "local" and (not baseline or not Path(baseline).is_dir()):
                raise RuntimeError("本地基线仓库目录无效，请填写已有 Git 仓库路径")
            if not baseline or not Path(baseline).is_dir():
                provisioned = self._provision_baseline(job)
                baseline = provisioned["path"]
            if not baseline or not Path(baseline).is_dir():
                raise RuntimeError("基线仓库目录无效")
            if ws.has_uncommitted(baseline):
                raise RuntimeError("基线仓库有未提交改动，请先在基线仓库提交后再准备任务")
            snap = ws.snapshot_baseline(baseline)
        self.store.update_job(job_id, {
            "baseline_repo": baseline,
            "baseline_sha": snap["sha"], "baseline_url": snap["url"],
            "baseline_branch": snap["branch"], "baseline_pushed": True,
            "harness_version": engines.cli_version(self.store.settings(), engines.job_engine(job)),
            "baseline_prepared": False, "status": "preparing",
        })
        pair_dir = self.store.workspace_pair_dir(job_id)
        job = self.store.get_job(job_id)
        results = {}
        for side_name in ("A", "B"):
            side = self.store.get_job(job_id)["sides"][side_name]
            if side.get("workspace") and Path(side["workspace"]).is_dir():
                results[side_name] = "kept"
                continue
            dest = pair_dir / side_name.lower()
            try:
                info = self._prepare_workspace(job, dest, side["branch"])
            except Exception as exc:
                self.store.update_job(job_id, {"status": "failed", "error": f"{side_name} 侧准备失败：{exc}"})
                raise
            self.store.update_side(job_id, side_name, {
                "workspace": info["workspace"], "status": "pending", "initial_sha": info["head"],
            })
            results[side_name] = "prepared"
        self.store.update_job(job_id, {"baseline_prepared": True, "status": "ready", "error": ""})
        return {"baseline": snap, "sides": results, "provisioned": provisioned}

    def _prepare_workspace(self, job: dict, dest: str | Path, branch: str) -> dict:
        if job.get("source_mode") == "github_commit":
            return baseline_mod.prepare_commit_side(
                job["baseline_repo"], dest, branch, job["baseline_sha"], agent=engines.job_engine(job),
            )
        return ws.prepare_side_workspace(
            job["baseline_repo"], dest, branch, job.get("copy_excludes", ""),
            baseline_sha=job["baseline_sha"],
            **({"agent": "codex"} if engines.job_engine(job) == "codex" else {}),
        )

    def run_side(self, job_id: str, side_name: str) -> None:
        key = f"{job_id}/{side_name}"
        self._clear_abort(key)
        settings = self.store.settings()
        job = self.store.get_job(job_id)
        managed_codex = _managed_codex(job)
        completion_mode = managed_codex and bool(settings.get("codex_completion_recovery"))
        side = job["sides"][side_name]
        wdir = side.get("workspace", "")
        if not wdir or not Path(wdir).is_dir():
            raise RuntimeError(f"{side_name} 侧工作区不存在，请先准备任务")

        evidence_dir = self.store.evidence_dir(job_id)
        log_path = evidence_dir / f"{side_name.lower()}-run.log"
        # Never let a stale low setting defeat gateway-cut retries.
        max_attempts = max(MIN_MAX_ATTEMPTS, min(MAX_SIDE_ATTEMPTS, max(
            1, int(settings.get("side_max_attempts", MIN_MAX_ATTEMPTS)))))
        if managed_codex:
            max_attempts = max(1, min(16 if completion_mode else 10, int(settings.get("codex_side_max_attempts", 3))))
        stall_after = stall_seconds_from_settings(settings)
        poll_every = max(5, int(settings.get("activity_poll_seconds", 15)))
        timeout = int(settings.get("side_timeout_seconds", 1800))
        wall = side_wall_seconds(settings)
        rng = random.Random(f"{job_id}/{side_name}")

        attempts_ledger = list(side.get("attempts") or [])
        # A crash can occur after writing the stream but before saving its
        # ledger row. Count and retain that attempt instead of overwriting it.
        recorded = {int(row["attempt"]) for row in attempts_ledger}
        for stream in sorted((evidence_dir / "attempts").glob(f"{side_name.lower()}-*-stream.jsonl")):
            match = re.fullmatch(r"[ab]-(\d+)-stream\.jsonl", stream.name)
            if match and int(match[1]) not in recorded:
                attempts_ledger.append({"attempt": int(match[1]), "status": "interrupted",
                                        "stream_path": str(stream), "session_id": self._stream_session_id(stream)})
        attempts_ledger.sort(key=lambda row: int(row["attempt"]))
        first_attempt = max((int(row["attempt"]) for row in attempts_ledger), default=0) + 1
        # Manual reruns receive a new bounded allowance. Attempt IDs remain
        # cumulative so old streams and transcripts can never be overwritten.
        budget_start = int(side.get("attempt_budget_start") or 0)
        last_attempt = budget_start + max_attempts
        started = time.time()
        deadline_key = "completion_deadline_epoch" if completion_mode else "clean_deadline_epoch"
        deadline = float(side.get(deadline_key) or started + wall)
        wall = min(wall, max(0, deadline - started))
        self.store.update_side(job_id, side_name, {
            "status": "running", "started_at": _stamp(), "error": "",
            "attempt_count": first_attempt - 1, "attempts": attempts_ledger,
            deadline_key: deadline, "completion_recovery": completion_mode,
        })
        if not attempts_ledger:
            log_path.write_text("", encoding="utf-8")
        result = None
        attempts_log = list(side.get("attempts_log") or [])
        # Keep abort requests throughout archive/backoff and later attempts.

        for attempt in range(first_attempt, last_attempt + 1):
            budget_attempt = attempt - budget_start
            if time.time() - started >= wall:
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(f"\n[watchdog] 单侧总预算 {wall}s 已用尽，停止重试\n")
                if result is None:
                    result = {
                        "completed": False, "code": None, "failure": "单侧总运行预算用尽",
                        "aborted": False, "new_session": None,
                    }
                else:
                    result = {**result, "completed": False,
                              "failure": result.get("failure") or "单侧总运行预算用尽"}
                break
            if self._is_aborted(key):
                break
            attempt_started = _stamp()
            if budget_attempt > 1:
                wait = (codex_retry_backoff_seconds(budget_attempt, rng) if managed_codex
                        else retry_backoff_seconds(budget_attempt, rng))
                with log_path.open("a", encoding="utf-8") as f:
                    action = "保留工作区并续接原会话" if completion_mode else "重新复制干净工作区"
                    f.write(f"\n\n=== 本轮第 {budget_attempt}/{max_attempts} 次尝试（累计 {attempt}）：{action}，{wait}s 后启动 ===\n")
                self.store.update_side(job_id, side_name, {"attempt_count": attempt})
                for _ in range(wait):
                    if self._is_aborted(key) or time.time() - started >= wall:
                        break
                    time.sleep(1)
                if self._is_aborted(key):
                    break
                if time.time() - started >= wall:
                    result = {"code": None, "aborted": False, "new_session": None,
                              **(result or {}), "completed": False, "failure": "单侧总运行预算用尽"}
                    break
                # Fresh copy from the baseline so a retry cannot mix two sessions'
                # edits into one product (single-turn evidence integrity).
                try:
                    if not completion_mode:
                        self._fresh_workspace(job_id, side_name, during_run=True)
                except Exception as exc:
                    # Never launch into the discarded turn's dirty workspace:
                    # skip this attempt and try another re-copy after backoff.
                    msg = (f"#{budget_attempt}/{max_attempts} 重新复制干净工作区失败：{exc}"
                           "（不在脏副本里启动，直接进入下一次重试）")
                    with log_path.open("a", encoding="utf-8") as f:
                        f.write(f"[retry] {msg}\n")
                    attempts_log.append(msg)
                    attempts_ledger.append({
                        "attempt": attempt, "started_at": attempt_started, "finished_at": _stamp(),
                        "duration_seconds": 0, "status": "recopy_failed", "failure": str(exc),
                        "session_id": "", "stream_path": "", "transcript_path": "",
                    })
                    continue
            # Mark running BEFORE launch: a recopy marks preparing (not pending)
            # so the UI keeps 中止 enabled and does not offer a conflicting 重跑.
            self.store.update_side(job_id, side_name, {"status": "running"})
            # Each attempt gets its own raw stream file, so discarded cut turns
            # stay auditable per attempt instead of being interleaved in one log.
            attempt_stream = evidence_dir / "attempts" / f"{side_name.lower()}-{attempt:02d}-stream.jsonl"
            attempt_stream.parent.mkdir(parents=True, exist_ok=True)
            attempt_stream.write_text("", encoding="utf-8")
            attempt_t0 = time.time()
            remaining = max(1, int(wall - (time.time() - started)))
            result = self._run_attempt(
                job_id, side_name, wdir, evidence_dir, log_path, attempt_stream,
                timeout=min(timeout, remaining), stall_after=stall_after, poll_every=poll_every,
                attempt=attempt,
            )
            if completion_mode:
                attempts_ledger = list(self.store.get_job(job_id)["sides"][side_name].get("attempts") or [])
            self._archive_attempt(job_id, side_name, evidence_dir, {
                **result,
                "attempt": attempt,
                "started_at": attempt_started,
                "finished_at": _stamp(),
                "duration_seconds": int(time.time() - attempt_t0),
            }, attempts_ledger)
            self.store.update_side(job_id, side_name, {
                "status": "running", "attempt_count": attempt, "attempts": attempts_ledger,
            })
            attempts_log.append(result["summary"])
            if result["aborted"]:
                break
            if result["completed"]:
                break
            if result.get("policy_failed"):
                break
            if managed_codex and not result.get("retryable", False):
                with log_path.open("a", encoding="utf-8") as f:
                    f.write("\n[retry] " + result.get("retry_stop_reason", "非临时接口错误或原因未明，停止自动重试，保留现场供检查") + "\n")
                break

        result = result or {"code": -1, "new_session": None, "completed": False,
                            "aborted": False, "summary": "未执行", "failure": "尝试次数预算已用尽"}
        attempt_aborted = bool(result.get("aborted")) or self._is_aborted(key)

        patch = {
            "exit_code": result["code"],
            "finished_at": _stamp(),
            "duration_seconds": int(time.time() - started),
            "attempts_log": attempts_log,
            "attempts": attempts_ledger,
        }

        completed = bool(result.get("completed"))
        chosen = None if attempt_aborted else (result.get("new_session") if completed else None)
        if chosen:
            kept = evidence_dir / f"{side_name.lower()}-{chosen['session_id']}.jsonl"
            shutil.copyfile(chosen["path"], kept)
            patch["session_id"] = chosen["session_id"]
            patch["jsonl_local"] = str(kept)

        if not attempt_aborted and completed and chosen:
            try:
                fin = ws.finalize_side(wdir, side["branch"], f"Pair {job_id} side {side_name} product",
                                       force=bool(side.get("retry_of")))
                patch["head_sha"] = fin["sha"]
                patch["head_url"] = fin["url"]
                patch["pushed"] = True
            except ws.GitError as exc:
                patch["error"] = f"产物提交/推送失败: {exc}"
        elif not attempt_aborted:
            # Incomplete rounds are discarded by the fresh-copy retry; a partial
            # commit on the branch would be force-overwritten by the next attempt.
            pass

        job = self.store.get_job(job_id)
        commands = [ln.strip() for ln in (job.get("check_commands") or "").splitlines() if ln.strip()]
        if attempt_aborted or self._is_aborted(key) or not commands or (completion_mode and not completed):
            patch["check_results"] = []
        else:
            patch["check_results"] = run_check_commands(
                commands, wdir, timeout=600, kill_job_factory=self._new_kill_job,
                should_abort=lambda: self._is_aborted(key),
            )

        aborted = (
            attempt_aborted
            or self._is_aborted(key)
            or any(r.get("aborted") for r in patch.get("check_results") or [])
        )
        if aborted:
            self._clear_abort(key)
            patch["status"] = "failed"
            patch["error"] = "已手动中止，可重跑该侧"
        elif not completed:
            last_failure = result.get("failure") or (
                "达到总超时" if result["code"] == 124 else
                f"退出码={result['code']}（多为网关 504/断流）" if result["code"] not in (0, None)
                else "未见 result:success")
            tries = sum(int(row["attempt"]) > budget_start for row in attempts_ledger)
            patch["status"] = "failed"
            if last_failure in ("单侧总运行预算用尽", "尝试次数预算已用尽"):
                retry_note = f"本轮已尝试 {tries} 次；可手动重跑以重新获得有限运行预算"
            else:
                retry_note = (
                    result.get("retry_stop_reason") or f"已停止自动重试（非临时接口错误或原因未明），本轮共尝试 {tries} 次"
                    if managed_codex and not result.get("retryable", False)
                    else f"本轮已尝试 {tries} 次（上限 {max_attempts}，{'保留进度续接' if completion_mode else '临时接口故障从干净基线重跑'}）仍未拿到完整轮次"
                )
            patch["error"] = (
                patch.get("error", "")
                + f" {last_failure}；{retry_note}，见 {log_path.name}"
            ).strip()
        elif not patch.get("session_id"):
            patch["status"] = "failed"
            patch["error"] = f"重试 {len(attempts_log)} 次后仍未产生完整的首轮会话（可能被断流截断），见 {log_path.name}"
        else:
            patch["status"] = "done"
        self.store.update_side(job_id, side_name, patch)

    @staticmethod
    def _pump_stream(proc: subprocess.Popen, logf, raw_path: Path, done: threading.Event,
                     signal: dict) -> None:
        """Read claude stream-json stdout: raw copy + a readable progress trace.

        ``signal`` is mutated with the terminal result subtype so the caller can
        distinguish a fully-completed turn (``result:success``) from a process
        that exited non-zero only because a trailing auxiliary call hit a 504.
        """
        seen: set[str] = set()

        def emit(line: str) -> None:
            key = line[:200]
            if key in seen:
                return
            seen.add(key)
            logf.write(line + "\n")
            logf.flush()

        try:
            with raw_path.open("a", encoding="utf-8") as raw:
                for line in proc.stdout:
                    raw.write(line if line.endswith("\n") else line + "\n")
                    raw.flush()
                    if "API Error:" in line and "504" in line:
                        signal["api_error_504"] = True
                    try:
                        ev = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        if signal.get("agent") == "codex" and "ERROR codex_core::" in line:
                            emit("[Codex diagnostic] " + line.strip()[:2000])
                            # A rejected tool call is returned to the model, which
                            # can continue the same turn. Raw diagnostics are not
                            # terminal events and must not poison completion.
                        continue
                    if not isinstance(ev, dict):
                        continue
                    if signal.get("agent") == "codex":
                        engines.consume_codex_event(ev, signal, emit)
                        continue
                    etype = ev.get("type")
                    # The very first stream-json event carries the authoritative
                    # session id ({"type":"system","subtype":"init","sessionId":…});
                    # capture it directly instead of inferring the session later
                    # from transcript mtimes (ambiguous across retries).
                    sid = ev.get("sessionId") or ev.get("session_id")
                    if sid and not signal.get("session_id"):
                        signal["session_id"] = str(sid)
                    if etype == "system" and ev.get("subtype") == "init" and ev.get("cwd"):
                        signal["cwd"] = str(ev["cwd"])
                    if etype == "system" and ev.get("subtype") == "init" and isinstance(ev.get("tools"), list):
                        from .compliance import CLAUDE_ALLOWED_TOOLS
                        unexpected = set(ev["tools"]) - CLAUDE_ALLOWED_TOOLS
                        signal["tools_verified"] = bool(ev["tools"]) and not unexpected
                        if unexpected:
                            signal["policy_error"] = "Claude 初始化工具超出正式运行允许清单：" + ", ".join(sorted(unexpected))
                    if etype == "result":
                        signal["result"] = ev.get("subtype")
                        signal["result_is_error"] = bool(ev.get("is_error", False))
                        emit(f"[result] {ev.get('subtype')} is_error={ev.get('is_error', False)} "
                             f"turns={ev.get('num_turns')} cost=${ev.get('total_cost_usd')}\n"
                             f"{str(ev.get('result', ''))[:3000]}")
                    elif etype == "assistant":
                        msg = ev.get("message", {})
                        blocks = msg.get("content", []) if isinstance(msg, dict) else []
                        for b in blocks:
                            if isinstance(b, dict) and b.get("type") == "tool_use":
                                inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                                emit(f"tool {b.get('name', '?')} {inp[:280]}")
                    elif etype == "user":
                        msg = ev.get("message", {})
                        content = msg.get("content", []) if isinstance(msg, dict) else []
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "tool_result":
                                    body = c.get("content", "")
                                    if isinstance(body, list):
                                        body = " ".join(
                                            x.get("text", "") for x in body if isinstance(x, dict)
                                        )
                                    mark = "TOOL-ERR" if c.get("is_error") else "tool-ok"
                                    emit(f"{mark} {str(body).replace(chr(10), ' ')[:200]}")
        except Exception as exc:
            signal["stream_error"] = str(exc)
        finally:
            done.set()

    @staticmethod
    def _stream_session_id(path: Path) -> str:
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict) and row.get("type") == "thread.started":
                        return str(row.get("thread_id") or "")
        except OSError:
            pass
        return ""

    def _completion_resume_id(self, job: dict, side_name: str, env: dict) -> str:
        """Bind recovery to this side's recorded id, cwd and original prompt."""
        side = job["sides"][side_name]
        sid = next((row["session_id"] for row in reversed(side.get("attempts") or [])
                    if row.get("session_id")), "")
        if not sid:
            return ""
        candidates = engines.find_codex_sessions(side["workspace"], env=env, session_id=sid)
        for candidate in candidates:
            evidence = engines.codex_evidence(candidate["path"], prompt=job["prompt"])
            if evidence.get("session_id") == sid and engines.normalized_prompt(job["prompt"]) in evidence.get("users", []):
                return sid
        raise RuntimeError("无法验证本侧原会话与工作区/提示词一致；已保留文件，停止续接")

    def _run_attempt(
        self, job_id: str, side_name: str, wdir: str, evidence_dir: Path, log_path: Path,
        attempt_stream: Path,
        *, timeout: int, stall_after: int, poll_every: int, attempt: int,
    ) -> dict:
        """Launch the selected CLI under the shared watchdog and process cleanup."""
        from .cli_config import KEY_ENV
        from .codex_relay import CodexRelay
        settings = self.store.settings()
        job = self.store.get_job(job_id)
        completion_mode = _managed_codex(job) and bool(settings.get("codex_completion_recovery"))
        agent = engines.job_engine(job)
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (settings.get("env_overrides") or {}).items()})
        env, connection_args = self.store.cli_connections.runtime(job, env)
        from .run_policy import preflight
        from .compliance import POLICY_VERSION
        capability = preflight(engines.cli_command(settings, agent), agent, env, connection_args, wdir)
        connection_args.extend(capability.pop("extra_args", []))
        capability["prompt_sha256"] = hashlib.sha256(job["prompt"].encode("utf-8")).hexdigest()
        cli_home = env["CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR"]
        if job.get("cli_home") != cli_home or job.get("execution_policy") != POLICY_VERSION:
            job = self.store.update_job(job_id, {"cli_home": cli_home, "execution_policy": POLICY_VERSION})
        relay = None
        binding = job.get("cli_connection") or {}
        if agent == "codex" and binding.get("mode") == "project":
            proxies = {k.lower().removesuffix("_proxy"): v for k, v in env.items()
                       if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
            relay = CodexRelay(
                binding["base_url"], env[KEY_ENV],
                attempt_stream.with_name(attempt_stream.stem + "-relay.jsonl"),
                proxies=proxies or None, progress_path=log_path,
                max_attempts=int(settings.get("codex_relay_max_attempts", 5)),
            )
        with relay if relay is not None else nullcontext():
            if relay is not None:
                # Per-process overrides: A/B retain the same on-disk config.
                env[KEY_ENV] = relay.token
                bypass = next((v for k, v in env.items() if k.lower() == "no_proxy"), "")
                for name in ("NO_PROXY", "no_proxy"):
                    env[name] = ",".join(filter(None, (bypass, "localhost", "127.0.0.1", "::1")))
                connection_args.extend([
                    "-c", "model_providers.pair_desk.base_url=" + json.dumps(relay.url),
                    "-c", "model_providers.pair_desk.stream_idle_timeout_ms=960000",
                ])
                if completion_mode or settings.get("codex_clean_single_turn", True):
                    retries = max(0, min(10, int(settings.get("codex_stream_max_retries", 5))))
                    connection_args.extend(["-c", f"model_providers.pair_desk.stream_max_retries={retries}"])
            result = self._execute_attempt(
                job_id, side_name, wdir, evidence_dir, log_path, attempt_stream,
                timeout=timeout, stall_after=stall_after, poll_every=poll_every,
                attempt=attempt, settings=settings, session_job=job, env=env,
                connection_args=connection_args,
                capability=capability,
            )
            if relay is not None:
                if relay.failure_reason and not result.get("completed"):
                    result["failure"] = "Codex 未完成：" + relay.failure_reason
                result["retryable"] = bool(not relay.permanent_failure.is_set()
                                            and (completion_retryable(result) or
                                                 (relay.exhausted.is_set() and not result.get("aborted")
                                                  and not result.get("completed"))))
                result["retry_stop_reason"] = (
                    "非可恢复错误或续接预算耗尽，保留工作区与全部尝试证据"
                    if completion_mode else
                    "非临时接口错误或重跑预算耗尽，失败证据已保留；禁止续接提示词")
            return result

    def _execute_attempt(
        self, job_id: str, side_name: str, wdir: str, evidence_dir: Path, log_path: Path,
        attempt_stream: Path, *, timeout: int, stall_after: int, poll_every: int,
        attempt: int, settings: dict, session_job: dict, env: dict,
        connection_args: list[str], capability: dict | None = None,
    ) -> dict:
        job = session_job
        agent = engines.job_engine(job)
        prompt = job["prompt"]
        session_job = job
        command = engines.cli_command(settings, agent)
        # bypassPermissions: -p 非交互下 acceptEdits 只放行文件编辑，Bash 会全部
        # 自动拒绝（pair-34679bd1de A 侧 35 次 python/pytest 被拦，产物零验证）。
        # 工作区本就是一次性基线副本，可整体丢弃，放行全部工具。
        permission_mode = settings.get("permission_mode", "bypassPermissions")
        args = [command, "-p", "--permission-mode", permission_mode,
                "--output-format", "stream-json", "--include-partial-messages",
                "--verbose"]
        stdin_data = None
        if agent == "codex":
            args = engines.codex_args(settings, job)
            if _managed_codex(job) and settings.get("codex_completion_recovery"):
                resume_id = self._completion_resume_id(job, side_name, env)
                if resume_id:
                    # Resume appends to the rollout. Snapshot crash orphans first.
                    ledger = list(job["sides"][side_name].get("attempts") or [])
                    row = next((r for r in reversed(ledger) if r.get("session_id") == resume_id), None)
                    if row is not None and not row.get("transcript_path"):
                        source = engines.find_codex_sessions(wdir, env=env, session_id=resume_id)[0]
                        kept = evidence_dir / "attempts" / f"{side_name.lower()}-{int(row['attempt']):02d}-{resume_id}.jsonl"
                        kept.parent.mkdir(parents=True, exist_ok=True)
                        if not kept.exists():
                            shutil.copyfile(source["path"], kept)
                        row["transcript_path"] = str(kept)
                        self.store.update_side(job_id, side_name, {"attempts": ledger})
                    args = engines.codex_resume_args(settings, job, resume_id)
                    self.store.update_side(job_id, side_name, {"resumed_session_id": resume_id})
            args[-1:-1] = connection_args
            stdin_data = COMPLETION_PROMPT if agent == "codex" and "resume" in args[1:3] else prompt
        else:
            args.extend(connection_args)
            from .compliance import CLAUDE_FLAGS
            args.extend(CLAUDE_FLAGS)
            if job.get("claude_model"):
                args.extend(["--model", job["claude_model"]])
            if len(prompt) < 1500:
                args.append(prompt)
            else:
                stdin_data = prompt

        if capability is not None:
            from .run_policy import write_receipt
            write_receipt(attempt_stream.with_name(attempt_stream.stem + "-policy.json"),
                          agent, env, capability, args)

        key = f"{job_id}/{side_name}"
        baseline_mtime = (time.time() if agent == "codex" else
                          self._latest_transcript_mtime(wdir, config_dir=job["cli_home"]) if job.get("cli_home") else
                          self._latest_transcript_mtime(wdir))
        t0 = time.time()
        stalls = 0
        killed_reason = ""

        with log_path.open("a", encoding="utf-8") as logf:
            if agent == "codex" and "resume" in args[1:3]:
                logf.write(f"[recovery] 续接原会话 {resume_id}；保留当前工作区，额外轮次写入轨迹\n")
            if attempt == 1:
                logf.write(f"$ {engines.ENGINE_LABELS[agent]} "
                           f"[prompt via {'argv' if stdin_data is None else 'stdin'}]\n\n")
            logf.flush()
            attempt_stream.parent.mkdir(parents=True, exist_ok=True)
            proc = subprocess.Popen(
                args, cwd=wdir, env=env,
                stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
                **procmon.hidden_console_kwargs(),
            )
            # Bind claude to a kill-on-close Job Object BEFORE it spawns any
            # tool child. Every descendant — including a Git-Bash `&`/`cmd start`
            # server that later reparents out of the PID tree — stays in the job
            # and is reaped when this attempt ends. This is what prevents the
            # leftover `node src/server.js` zombie ports after the run.
            job = self._new_kill_job()
            job_ok = job.add_pid(proc.pid) if job.alive else False
            if not job_ok:
                logf.write("[watchdog] Linux 独立进程组清理已启用；服务退出时 systemd 清理整个服务组\n"
                           if os.name != "nt" else
                           f"[watchdog] Job Object 绑定不可用（{job.reason}），后台服务将退化为 taskkill /T 尽力清理，可能残留\n")
                logf.flush()

            def reap() -> None:
                """Stop the whole attempt: job first (catches detached nodes),
                then taskkill /T as a belt-and-braces fallback."""
                if job_ok:
                    job.terminate()
                procmon.kill_tree(proc.pid)
            pump_done: threading.Event | None = None
            reg: Path | None = None
            try:
                pump_done = threading.Event()
                signal: dict = {"agent": agent}
                pump = threading.Thread(
                    target=self._pump_stream,
                    args=(proc, logf, attempt_stream, pump_done, signal),
                    daemon=True,
                )
                pump.start()
                with self._lock:
                    self._procs[key] = proc
                # Register before feeding stdin so 中止 can terminate a child
                # that is still blocked on a huge prompt write.
                if stdin_data is not None:
                    _feed_stdin(proc, stdin_data)
                reg = self._reg_path(job_id, side_name)
                reg.write_text(json.dumps({
                    "job_id": job_id, "side": side_name, "pid": proc.pid,
                    "workspace": wdir, "started": _stamp(),
                    "process_start": procmon.snapshot().get(proc.pid, {}).get("start_time", ""),
                }, ensure_ascii=False), encoding="utf-8")
                prev_snap = procmon.snapshot()
                idle_for = 0.0
                last_mtime = baseline_mtime
                code = None
                while True:
                    if signal.get("policy_error"):
                        reap()
                        _wait_proc(proc, timeout=30)
                        killed_reason = "policy-error"
                        break
                    if proc.poll() is not None:
                        code = proc.returncode
                        break
                    for _ in range(poll_every):
                        if proc.poll() is not None or self._is_aborted(key):
                            break
                        time.sleep(1)
                    if proc.poll() is not None:
                        code = proc.returncode
                        break
                    if self._is_aborted(key):
                        reap()
                        _wait_proc(proc, timeout=30)
                        killed_reason = "manual-abort"
                        break
                    if time.time() - t0 > timeout:
                        reap()
                        _wait_proc(proc, timeout=30)
                        killed_reason = "total-timeout"
                        break
                    cur_snap = procmon.snapshot()
                    tree = procmon.descendants(proc.pid, cur_snap)
                    cpu_io_busy = procmon.tree_busy(prev_snap, cur_snap, tree, proc.pid)
                    stream_mtime = attempt_stream.stat().st_mtime if attempt_stream.is_file() else 0.0
                    if agent == "codex":
                        cur_mtime = stream_mtime
                        relay_log = attempt_stream.with_name(attempt_stream.stem + "-relay.jsonl")
                        if relay_log.is_file():
                            cur_mtime = max(cur_mtime, relay_log.stat().st_mtime)
                    else:
                        transcript_mtime = (self._latest_transcript_mtime(wdir, config_dir=session_job["cli_home"])
                                            if session_job.get("cli_home") else self._latest_transcript_mtime(wdir))
                        cur_mtime = max(transcript_mtime, stream_mtime)
                    idle_for, last_mtime = watchdog_idle_seconds(
                        idle_for, cpu_io_busy=cpu_io_busy,
                        prev_transcript_mtime=last_mtime,
                        cur_transcript_mtime=cur_mtime,
                        poll_every=poll_every,
                    )
                    prev_snap = cur_snap
                    # Re-read so turning stall off in settings applies to in-flight sides.
                    stall_after = stall_seconds_from_settings(self.store.settings())
                    if stall_after > 0 and idle_for >= stall_after:
                        stalls += 1
                        logf.write(f"\n[watchdog] {int(idle_for)}s 无 CPU/IO/轨迹活动，判定网关断流，"
                                   "终止本次尝试并自动重试\n")
                        logf.flush()
                        reap()
                        _wait_proc(proc, timeout=30)
                        killed_reason = "stall"
                        break
            finally:
                with self._lock:
                    self._procs.pop(key, None)
                if reg is not None:
                    try:
                        reg.unlink()
                    except OSError:
                        pass
                # Reap FIRST so a leftover grandchild drops the stdout write
                # end; then the pump can see EOF. Never stream.close() or
                # os.close(fd) from this thread — both can deadlock against a
                # pump blocked in readline() (CPython IO lock / Win32 pipe).
                try:
                    if job_ok:
                        job.terminate()
                    elif os.name != "nt" or proc.poll() is None:
                        procmon.kill_tree(proc.pid)
                finally:
                    job.close()
                if pump_done is not None:
                    pump_done.wait(timeout=15)
        duration = int(time.time() - t0)
        if killed_reason == "total-timeout":
            code = 124
        elif code is None:
            code = -1
        # Pick the newest fresh transcript that is genuinely complete. A session
        # whose tail is a gateway API Error or a dangling tool_result is a cut
        # turn and can never be accepted, regardless of the CLI's exit code or
        # of a trailing result:success event.
        init_sid = signal.get("session_id") or ""
        if agent == "codex":
            candidates = engines.find_codex_sessions(
                wdir, env=env, session_id=init_sid, since=t0 - 1,
            ) if init_sid and killed_reason != "manual-abort" else []
        else:
            candidates = [
                s for s in engines.find_job_sessions(session_job, wdir, settings)
                if float(s["mtime"]) >= baseline_mtime - 1
            ] if killed_reason != "manual-abort" else []
        if init_sid and candidates:
            # Prefer the exact session the CLI announced in its init event;
            # mtime order alone is ambiguous when several retries share a cwd.
            exact = [s for s in candidates if s.get("session_id") == init_sid]
            if exact:
                candidates = exact + [s for s in candidates if s.get("session_id") != init_sid]
        new_session = None
        cutoff_session = None
        cutoff_reason = ""
        for s in candidates:
            if agent == "codex":
                evidence = engines.codex_evidence(s["path"], prompt=prompt,
                    allow_recovered_turns=bool(session_job["sides"][side_name].get("completion_recovery")))
                reason = evidence["reason"]
                if not settings.get("codex_completion_recovery") and evidence.get("user_turn_count", len(evidence["users"])) != 1:
                    reason = "multiple_user_turns"
                if reason is None and engines.normalized_prompt(prompt) not in evidence["users"]:
                    reason = "prompt_mismatch"
            else:
                reason = ws.transcript_interruption_reason(s["path"])
            from .compliance import audit_trace, completion_reason
            compliance = audit_trace(s["path"])
            if completion_reason(compliance):
                reason = completion_reason(compliance)
            if reason is None:
                new_session = s
                break
            # candidates are newest first; remember the newest cutoff reason.
            if not cutoff_reason:
                cutoff_reason = reason
                cutoff_session = s
        chosen_for_ledger = new_session or cutoff_session
        # The CLI emits subtype=success even for a fatal API error, distinguished
        # by is_error=true; and an error-during-run leaves no usable result at
        # all. Only a clean result together with a complete transcript counts.
        result_ok = signal.get("result") == "success" and not signal.get("result_is_error")
        if agent == "claude" and capability is not None and result_ok and not signal.get("tools_verified"):
            signal["policy_error"] = signal.get("policy_error") or "Claude 未提供可验证的初始化工具清单"
        completed = result_ok and bool(new_session) and not signal.get("policy_error")
        if agent == "codex":
            completed = completed and code == 0 and not killed_reason and not signal.get("stream_error")
        failure = "" if completed else PairRunner._classify_attempt(
            killed_reason, code, signal, cutoff_reason, bool(candidates))
        if agent == "codex" and not completed:
            failure = (f"Codex 未完成：{signal.get('error_message') or killed_reason or cutoff_reason or signal.get('stream_error') or signal.get('result') or '缺少完成事件/本次 rollout'}"
                       f"（exit={code}）")
        if signal.get("policy_error"):
            failure = signal["policy_error"]
        summary = (f"#{attempt} exit={code} {duration}s stalls={stalls} "
                   f"{'完成' if completed else ('有会话未完成' if new_session else '无新会话')}"
                   + (f" [原因: {failure}]" if failure else ""))
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n[attempt] {summary} killed={killed_reason or '-'} "
                       f"result={signal.get('result', '-')} is_error={signal.get('result_is_error', False)} "
                       f"504={signal.get('api_error_504', False)} sid={init_sid or '-'}\n")
        return {
            "code": code, "new_session": new_session, "completed": completed,
            "duration": duration, "failure": failure,
            "stalls": stalls, "aborted": killed_reason == "manual-abort",
            "summary": summary,
            "init_session_id": init_sid,
            "agent": agent,
            "retryable": bool(
                signal.get("result") == "failed" and signal.get("retryable")
                and not killed_reason and not signal.get("stream_error")
            ),
            "policy_failed": bool(cutoff_reason.startswith("extra_ai") or signal.get("policy_error")),
            # The transcript this attempt actually produced (complete or cut);
            # used by the caller to archive per-attempt evidence.
            "session_path": (chosen_for_ledger or {}).get("path", ""),
            "session_id": (chosen_for_ledger or {}).get("session_id", "") or init_sid,
        }

    def _archive_attempt(
        self, job_id: str, side_name: str, evidence_dir: Path,
        result: dict, ledger: list[dict],
    ) -> None:
        """Snapshot this attempt's transcript into evidence and append a ledger row.

        The winning transcript is later re-copied as ``<side>-<sid>.jsonl`` and
        bound to the side; cut/discarded attempts stay here under ``attempts/``
        so every discarded turn remains auditable (A-001 must not disappear).
        """
        attempt = int(result["attempt"])
        transcript_path = ""
        session_id = result.get("session_id", "")
        src = result.get("session_path", "")
        if src and Path(src).is_file():
            kept = evidence_dir / "attempts" / (
                f"{side_name.lower()}-{attempt:02d}-{session_id or 'session'}.jsonl")
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, kept)
            transcript_path = str(kept)
        ledger.append({
            "attempt": attempt,
            "agent": result.get("agent", engines.job_engine(self.store.get_job(job_id))),
            "started_at": result.get("started_at", ""),
            "finished_at": result.get("finished_at", ""),
            "duration_seconds": result.get("duration_seconds", 0),
            "status": "aborted" if result.get("aborted") else ("complete" if result.get("completed") else "cut"),
            "exit_code": result.get("code"),
            "stalls": result.get("stalls", 0),
            "failure": result.get("failure", ""),
            "session_id": session_id,
            "stream_path": str(evidence_dir / "attempts" / f"{side_name.lower()}-{attempt:02d}-stream.jsonl"),
            "transcript_path": transcript_path,
        })

    @staticmethod
    def _classify_attempt(killed_reason: str, code, signal: dict,
                          cutoff_reason: str, has_candidates: bool) -> str:
        """Human-readable, stable reason why an attempt did not complete.

        Order matters: a watchdog kill and a total timeout are positive local
        observations; otherwise the transcript tail (gateway API Error / dangling
        tool result) is the strongest evidence of an upstream cut.
        """
        if killed_reason == "stall":
            return "网关静默断流（看门狗无活动终止）"
        if killed_reason == "total-timeout":
            return "单侧总超时"
        if cutoff_reason == "api_error" or signal.get("api_error_504"):
            return "网关 504/API Error，轮次中途截断"
        if cutoff_reason == "dangling_tool_result":
            return "流被截断（停在工具返回后无收尾）"
        if cutoff_reason == "no_assistant":
            return "会话无模型回复（启动即断流）"
        if cutoff_reason.startswith("extra_ai"):
            return "轨迹合规检查未通过：" + cutoff_reason + "；保留原件，禁止标记成功"
        if signal.get("result_is_error"):
            return f"CLI 上报 result 错误（subtype={signal.get('result', '?')}）"
        if code not in (0, None):
            return f"claude 退出码 {code}（多为网关错误）"
        if not has_candidates:
            return "未产生新会话"
        return "未见 result:success"

    @staticmethod
    def _latest_transcript_mtime(wdir: str, *, config_dir: str = "") -> float:
        from . import procmon
        home = Path(config_dir or os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        return procmon.newest_jsonl_mtime(ws.native_path(home / "projects"), ws._encode_cwd(wdir))


    def _fresh_workspace(self, job_id: str, side_name: str, *, during_run: bool = False) -> dict:
        """Archive one side workspace and re-copy it from the baseline."""
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "") or str(
            self.store.workspace_pair_dir(job_id) / side_name.lower())
        if Path(wdir).exists():
            # Retain the failed artifact separately; never use its files as input
            # to the next attempt, and never overwrite earlier attempt evidence.
            archive = self.store.evidence_dir(job_id) / "discarded-workspaces"
            archive.mkdir(parents=True, exist_ok=True)
            destination = archive / f"{side_name.lower()}-{time.time_ns()}"
            source = Path(wdir).resolve()
            if source == archive.resolve() or source in archive.resolve().parents:
                raise RuntimeError("工作区归档路径不能位于源目录内")
            shutil.move(str(source), str(destination))
        fresh = self._prepare_workspace(job, wdir, side["branch"])
        # during_run: keep the side visibly busy. Writing pending here is what
        # made the UI offer 「重跑」 while retry_side still saw a live worker.
        self.store.update_side(job_id, side_name, {
            "workspace": fresh["workspace"],
            "initial_sha": fresh["head"],
            "status": "preparing" if during_run else "pending",
            "error": "",
            "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "",
            "trace_url": "", "pushed": False, "exit_code": None,
            "video_local": "", "video_url": "", "demo": {},
            "failure_capture": {}, "check_results": [],
            "completion_recovery": False, "resumed_session_id": "",
            "retry_of": side.get("head_sha") or "1",
        })
        return fresh

    def _is_aborted(self, key: str) -> bool:
        with self._lock:
            return key in self._abort

    def _clear_abort(self, key: str) -> None:
        with self._lock:
            self._abort.discard(key)

    def abort_side(self, job_id: str, side_name: str) -> None:
        """Request termination of a side.

        Works both while the CLI is running (terminate the process tree via the
        watchdog loop) and while the side is sleeping in retry backoff (the
        1-second backoff loop consumes the flag). A stale flag from a previous
        run of this side is cleared once at the start of run_side, not between
        attempts (abort during archive/backoff must survive).
        """
        key = f"{job_id}/{side_name}"
        with self._lock:
            proc = self._procs.get(key)
            self._abort.add(key)
            if proc is not None:
                proc.terminate()

    def retry_side(self, job_id: str, side_name: str, *, enqueue: bool = True,
                   renew_budget: bool = True) -> None:
        with self._lock:
            if self.is_side_running(job_id, side_name):
                raise RuntimeError(
                    f"{side_name} 侧正在运行，不能重跑。请先点「中止」等它结束（状态变成失败/完成）后再重跑。"
                )
            job = self.store.get_job(job_id)
            if job.get("archived") or DeskStore.review_locked(job):
                raise RuntimeError("任务已归档或评审已锁定，请先恢复或解锁再重跑")
            completion_mode = _managed_codex(job) and bool(self.store.settings().get("codex_completion_recovery"))
            patch = {"status": "preparing", "error": ""}
            if renew_budget:
                side = job["sides"][side_name]
                capture = side.get("failure_capture") or {}
                if completion_mode and capture.get("origin") == "posthoc-failed-worktree":
                    canonical = self.store.workspace_pair_dir(job_id) / side_name.lower()
                    original = capture.get("source_workspace")
                    if (not canonical.is_dir() or not original
                            or Path(original).resolve() != canonical.resolve()):
                        raise RuntimeError("失败快照的原工作区不存在或路径不一致；请关闭续接，使用干净基线重跑")
                evidence = self.store.evidence_dir(job_id)
                previous_attempt = max((int(row["attempt"]) for row in side.get("attempts") or []), default=0)
                # Include a crashed attempt whose stream exists but ledger was not saved.
                for stream in (evidence / "attempts").glob(f"{side_name.lower()}-*-stream.jsonl"):
                    match = re.fullmatch(r"[ab]-(\d+)-stream\.jsonl", stream.name)
                    if match:
                        previous_attempt = max(previous_attempt, int(match[1]))
                self.store.begin_manual_retry(job_id, side_name, previous_attempt)
            else:
                self.store.update_side(job_id, side_name, patch)
        if completion_mode:
            self.store.update_side(job_id, side_name, {"status": "pending", "error": "", "completion_recovery": True})
            self._sync_job_status(job_id)
            if enqueue:
                self.enqueue(job_id)
            return
        # Mark busy before the slow recopy so a second click cannot rmtree
        # a workspace this call or a sibling worker is currently copying.
        self.store.update_side(job_id, side_name, {"status": "preparing", "error": ""})
        try:
            self._fresh_workspace(job_id, side_name, during_run=True)
        except Exception as exc:
            self.store.update_side(job_id, side_name, {"status": "failed", "error": f"重跑准备失败：{exc}"})
            self._sync_job_status(job_id)
            raise
        self._sync_job_status(job_id)
        if enqueue:
            self.enqueue(job_id)

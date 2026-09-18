"""Background execution engine for A/B pairs.

- one queue, bounded parallelism (max parallel pairs + both sides concurrent)
- the model itself runs exactly once per side with the frozen prompt
- harness/model configuration is inherited from the user's environment
  (cc-switch / global settings); this engine never sets ANTHROPIC_MODEL
- job state lives on disk, so a crashed process can resume on next start:
  running sides become 'failed' and can be retried by re-copying the workspace
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import workspace as ws
from .desk_store import DeskStore


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def claude_version(command: str = "claude") -> str:
    try:
        p = subprocess.run([command, "--version"], capture_output=True, text=True, timeout=15)
        return p.stdout.strip() if p.returncode == 0 else ""
    except OSError:
        return ""


class PairRunner:
    def __init__(self, store: DeskStore):
        self.store = store
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._workers: list[threading.Thread] = []
        self._active: dict[str, bool] = {}
        self._queued: set[str] = set()
        self._procs: dict[str, subprocess.Popen] = {}
        self._abort: set[str] = set()
        self._lock = threading.RLock()

    # ---------- lifecycle ----------
    def recover(self) -> dict:
        """Mark unfinished work after a crash so the UI shows retryable state."""
        recovered = []
        for job in self.store.list_jobs():
            changed = False
            for side_name, side in job["sides"].items():
                if side["status"] in ("preparing", "running", "collecting"):
                    self.store.update_side(job["id"], side_name, {
                        "status": "failed",
                        "error": "进程中断，工作区状态未知，请重跑该侧（会从基线重新复制）",
                        "finished_at": _stamp(),
                    })
                    changed = True
                    recovered.append(f"{job['id']}/{side_name}")
            if job["status"] == "running":
                self.store.update_job(job["id"], {"status": "ready" if changed else job["status"]})
        return {"recovered": recovered}

    def start(self) -> None:
        if self._workers:
            return
        self.recover()
        self._stop.clear()
        count = max(1, int(self.store.settings().get("max_parallel_pairs", 2)))
        for i in range(count):
            t = threading.Thread(target=self._worker_loop, name=f"pair-worker-{i}", daemon=True)
            t.start()
            self._workers.append(t)

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, job_id: str) -> None:
        with self._lock:
            if self._active.get(job_id) or job_id in self._queued:
                return
            self._queued.add(job_id)
        self._queue.put(job_id)

    def active_jobs(self) -> list[str]:
        with self._lock:
            return [jid for jid, on in self._active.items() if on]

    # ---------- worker ----------
    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            with self._lock:
                self._active[job_id] = True
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

    def _run_job(self, job_id: str) -> None:
        job = self.store.get_job(job_id)
        if not job.get("baseline_sha"):
            raise RuntimeError("任务尚未准备（缺少基线快照）")
        # Skip sides that already finished (e.g. single-side retry); skip whole done jobs.
        if job.get("status") == "evidence_ready":
            return
        self.store.update_job(job_id, {"status": "running", "error": ""})
        threads = []
        for side_name in ("A", "B"):
            if job["sides"][side_name]["status"] == "done":
                continue
            t = threading.Thread(target=self._run_side_safe, args=(job_id, side_name), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        job = self.store.get_job(job_id)
        statuses = {s: job["sides"][s]["status"] for s in ("A", "B")}
        if all(v == "done" for v in statuses.values()):
            self.store.update_job(job_id, {"status": "evidence_ready"})
        elif any(v == "failed" for v in statuses.values()):
            self.store.update_job(job_id, {"status": "failed"})
        else:
            self.store.update_job(job_id, {"status": "ready"})

    def _run_side_safe(self, job_id: str, side_name: str) -> None:
        try:
            self.run_side(job_id, side_name)
        except Exception as exc:
            self.store.update_side(job_id, side_name, {
                "status": "failed", "error": str(exc), "finished_at": _stamp(),
            })

    # ---------- per-side pipeline ----------
    def prepare(self, job_id: str) -> dict:
        """Snapshot the baseline repo and copy A/B workspaces. Idempotent-ish."""
        job = self.store.get_job(job_id)
        baseline = job.get("baseline_repo", "")
        if not baseline or not Path(baseline).is_dir():
            raise RuntimeError("基线仓库目录无效")
        if ws.has_uncommitted(baseline):
            raise RuntimeError("基线仓库有未提交改动，请先在基线仓库提交后再准备任务")
        snap = ws.snapshot_baseline(baseline)
        self.store.update_job(job_id, {
            "baseline_sha": snap["sha"], "baseline_url": snap["url"],
            "baseline_branch": snap["branch"], "baseline_pushed": True,
            "harness_version": claude_version(self.store.settings().get("claude_command", "claude")),
            "status": "ready",
        })
        pair_dir = self.store.workspace_pair_dir(job_id)
        results = {}
        for side_name in ("A", "B"):
            side = self.store.get_job(job_id)["sides"][side_name]
            if side.get("workspace") and Path(side["workspace"]).is_dir():
                results[side_name] = "kept"
                continue
            dest = pair_dir / side_name.lower()
            info = ws.prepare_side_workspace(
                baseline, dest, side["branch"], job.get("copy_excludes", ""),
            )
            self.store.update_side(job_id, side_name, {"workspace": info["workspace"], "status": "pending"})
            results[side_name] = "prepared"
        return {"baseline": snap, "sides": results}

    def run_side(self, job_id: str, side_name: str) -> None:
        settings = self.store.settings()
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "")
        if not wdir or not Path(wdir).is_dir():
            raise RuntimeError(f"{side_name} 侧工作区不存在，请先准备任务")

        evidence_dir = self.store.evidence_dir(job_id)
        log_path = evidence_dir / f"{side_name.lower()}-run.log"

        self.store.update_side(job_id, side_name, {"status": "running", "started_at": _stamp(), "error": ""})

        env = dict(os.environ)
        # Inherit the user's full environment: cc-switch and the global
        # ~/.claude/settings.json (auto_model/urm, gateway, 1M context) are what
        # the child claude.exe must use. This engine never selects a model.
        # env_overrides in desk settings is reserved for rare explicit needs.
        over = settings.get("env_overrides") or {}
        env.update({str(k): str(v) for k, v in over.items()})

        command = settings.get("claude_command", "claude")
        timeout = int(settings.get("side_timeout_seconds", 1800))
        prompt = job["prompt"]
        args = [command, "-p", "--permission-mode", "acceptEdits", "--verbose"]
        # short prompt via argv keeps the shell quoting trivial; long prompt via temp file + stdin
        started = time.time()
        with log_path.open("w", encoding="utf-8") as logf:
            logf.write(f"$ {' '.join(args)}  [prompt via {'argv' if len(prompt) < 1500 else 'stdin'}]\n\n")
            logf.flush()
            run_args = args
            stdin_data = None
            if len(prompt) < 1500:
                run_args = args + [prompt]
            else:
                stdin_data = prompt
            proc = subprocess.Popen(
                run_args, cwd=wdir, env=env,
                stdin=subprocess.PIPE if stdin_data is not None else None,
                stdout=logf, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            with self._lock:
                self._procs[f"{job_id}/{side_name}"] = proc
            try:
                proc.communicate(input=stdin_data, timeout=timeout)
                code = proc.returncode
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate(timeout=30)
                code = 124
                logf.write(f"\n[TIMEOUT after {timeout}s]\n")
            finally:
                with self._lock:
                    self._procs.pop(f"{job_id}/{side_name}", None)

        finished = _stamp()
        patch = {"exit_code": code, "finished_at": finished, "duration_seconds": int(time.time() - started)}
        aborted = f"{job_id}/{side_name}" in self._abort
        if aborted:
            self._abort.discard(f"{job_id}/{side_name}")

        # collect the session that ran in this workspace, newest one created after side start
        sessions = ws.find_session_jsonl(wdir)
        if sessions and not aborted:
            chosen = sessions[0]
            kept = evidence_dir / f"{side_name.lower()}-{chosen['session_id']}.jsonl"
            shutil.copyfile(chosen["path"], kept)
            patch["session_id"] = chosen["session_id"]
            patch["jsonl_local"] = str(kept)

        # auto commit + push the product
        if not aborted and code in (0, 1):
            try:
                fin = ws.finalize_side(wdir, side["branch"], f"Pair {job_id} side {side_name} product")
                patch["head_sha"] = fin["sha"]
                patch["head_url"] = fin["url"]
                patch["pushed"] = True
            except ws.GitError as exc:
                patch["error"] = f"产物提交/推送失败: {exc}"
        else:
            # even failed runs keep whatever commit exists (evidence of failure)
            if not aborted:
                sha = ws.head_sha(wdir)
                if sha and ws.is_sha40(sha):
                    try:
                        ws.finalize_side(wdir, side["branch"], f"Pair {job_id} side {side_name} (failed run)")
                        patch["head_sha"] = sha
                        patch["head_url"] = ws.commit_permalink(wdir, sha)
                        patch["pushed"] = True
                    except ws.GitError:
                        pass

        # optional verification commands (build/test) — advisory, never blocks evidence
        commands = [ln.strip() for ln in (job.get("check_commands") or "").splitlines() if ln.strip()]
        check_results = []
        if not aborted:
            for command_line in commands:
                try:
                    p = subprocess.run(
                        command_line, cwd=wdir, shell=True, text=True,
                        encoding="utf-8", errors="replace",
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600,
                    )
                    check_results.append({"command": command_line, "exit_code": p.returncode})
                except subprocess.TimeoutExpired:
                    check_results.append({"command": command_line, "exit_code": 124})
                except OSError as exc:
                    check_results.append({"command": command_line, "exit_code": 127, "error": str(exc)})
        patch["check_results"] = check_results
        if aborted:
            patch["status"] = "failed"
            patch["error"] = "已手动中止，可重跑该侧"
        elif code not in (0, 1):
            patch["status"] = "failed"
            patch["error"] = (patch.get("error", "") + f" claude 退出码={code}，见 {log_path.name}").strip()
        elif not patch.get("session_id"):
            patch["status"] = "failed"
            patch["error"] = "执行结束但没有在 ~/.claude/projects 找到该目录的会话记录"
        else:
            patch["status"] = "done"
        self.store.update_side(job_id, side_name, patch)

    def abort_side(self, job_id: str, side_name: str) -> None:
        """Terminate a running side; it lands as failed and can be re-copied/retried."""
        with self._lock:
            proc = self._procs.get(f"{job_id}/{side_name}")
            if proc is not None:
                self._abort.add(f"{job_id}/{side_name}")
                proc.terminate()
            else:
                raise RuntimeError(f"{side_name} 侧当前没有运行中的进程")

    def retry_side(self, job_id: str, side_name: str) -> None:
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "") or str(self.store.workspace_pair_dir(job_id) / side_name.lower())
        if Path(wdir).exists():
            shutil.rmtree(wdir)
        fresh = ws.prepare_side_workspace(
            job["baseline_repo"], wdir, side["branch"], job.get("copy_excludes", ""),
        )
        self.store.update_side(job_id, side_name, {
            "workspace": fresh["workspace"], "status": "pending", "error": "",
            "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "",
            "trace_url": "", "pushed": False, "exit_code": None,
        })
        self.store.update_job(job_id, {"status": "ready"})
        self.enqueue(job_id)

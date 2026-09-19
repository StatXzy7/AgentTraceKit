"""Background execution engine for A/B pairs.

- one queue, bounded parallelism (max parallel pairs + both sides concurrent)
- the model itself runs exactly once per side with the frozen prompt
- harness/model configuration is inherited from the user's environment
  (cc-switch / global settings); this engine never sets ANTHROPIC_MODEL
- job state lives on disk, so a crashed process can resume on next start:
  running sides become 'failed' and can be retried by re-copying the workspace

Upstream gateway cuts (silent SSE stalls, mid-turn 504, truncated transcripts)
are treated as transient infrastructure faults, never as model results: the
attempt is classified, thrown away, the workspace is re-copied from the
baseline and the side is relaunched with exponential backoff until a complete
turn is produced or the attempt cap is reached.
"""
from __future__ import annotations

import json
import os
import queue
import random
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import ghutil
from . import workspace as ws
from .desk_store import DeskStore

# Minimum attempts even if settings.json carries an older, smaller value:
# upstream instability must never silently cap retries at a stale number.
MIN_MAX_ATTEMPTS = 12
# Cap a single retry wait so a side stays responsive to manual abort.
MAX_BACKOFF_SECONDS = 120


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
    def _reg_path(self, job_id: str, side_name: str) -> Path:
        d = self.store.home / "running"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{job_id}__{side_name}.json"

    def is_side_running(self, job_id: str, side_name: str) -> bool:
        proc = self._procs.get(f"{job_id}/{side_name}")
        return proc is not None and proc.poll() is None

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
        for job in self.store.list_jobs():
            changed = False
            for side_name, side in job["sides"].items():
                # A pending side normally means "prepared, not enqueued yet";
                # but a live registry left for it means the crash happened in
                # the tiny window after fresh-copy and before status=running.
                in_flight = side["status"] in ("preparing", "running", "collecting") or (
                    side["status"] == "pending" and f"{job['id']}/{side_name}" in registered)
                if in_flight:
                    self.store.update_side(job["id"], side_name, {
                        "status": "failed",
                        "error": ("交付台重启，已终止上次未完成的运行；点「重跑该侧」从基线重新执行"
                                  + ("（旧进程已清理）" if any(x.startswith(f"{job['id']}/{side_name}") for x in killed) else "")),
                        "finished_at": _stamp(),
                    })
                    changed = True
                    recovered.append(f"{job['id']}/{side_name}")
            if job["status"] == "running":
                self.store.update_job(job["id"], {"status": "ready" if changed else job["status"]})
        return {"recovered": recovered, "orphans_killed": killed}

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
        self._sync_job_status(job_id)

    def _sync_job_status(self, job_id: str) -> None:
        """Derive the job-level status from the two sides' statuses.

        A single-side retry while the other side is still running must not
        downgrade the job tag to 待运行: pending/failed sides only win when
        nothing is running.
        """
        job = self.store.get_job(job_id)
        statuses = [job["sides"][s]["status"] for s in ("A", "B")]
        if all(v == "done" for v in statuses):
            new_status = "evidence_ready"
        elif any(v == "failed" for v in statuses):
            new_status = "failed"
        elif any(v in ("running", "preparing", "collecting") for v in statuses):
            new_status = "running"
        else:
            new_status = "ready"
        if job.get("status") != new_status:
            self.store.update_job(job_id, {"status": new_status})

    def _run_side_safe(self, job_id: str, side_name: str) -> None:
        try:
            self.run_side(job_id, side_name)
        except Exception as exc:
            self.store.update_side(job_id, side_name, {
                "status": "failed", "error": str(exc), "finished_at": _stamp(),
            })

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
        """Snapshot the baseline repo and copy A/B workspaces. Idempotent-ish."""
        job = self.store.get_job(job_id)
        baseline = job.get("baseline_repo", "")
        provisioned: dict | None = None
        if not baseline or not Path(baseline).is_dir():
            provisioned = self._provision_baseline(job)
            baseline = provisioned["path"]
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
        return {"baseline": snap, "sides": results, "provisioned": provisioned}

    def run_side(self, job_id: str, side_name: str) -> None:
        settings = self.store.settings()
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "")
        if not wdir or not Path(wdir).is_dir():
            raise RuntimeError(f"{side_name} 侧工作区不存在，请先准备任务")

        evidence_dir = self.store.evidence_dir(job_id)
        log_path = evidence_dir / f"{side_name.lower()}-run.log"
        # Never let a stale low setting defeat gateway-cut retries.
        max_attempts = max(MIN_MAX_ATTEMPTS, max(1, int(settings.get("side_max_attempts", MIN_MAX_ATTEMPTS))))
        stall_after = max(30, int(settings.get("stall_seconds", 240)))
        poll_every = max(5, int(settings.get("activity_poll_seconds", 15)))
        timeout = int(settings.get("side_timeout_seconds", 1800))
        rng = random.Random(f"{job_id}/{side_name}")

        self.store.update_side(job_id, side_name, {
            "status": "running", "started_at": _stamp(), "error": "", "attempts": 1,
        })
        log_path.write_text("", encoding="utf-8")
        started = time.time()
        result = None
        attempts_log: list[str] = []
        key = f"{job_id}/{side_name}"

        for attempt in range(1, max_attempts + 1):
            self._abort.discard(key)
            if attempt > 1:
                wait = retry_backoff_seconds(attempt, rng)
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(f"\n\n=== 第 {attempt}/{max_attempts} 次尝试：重新复制干净工作区，{wait}s 后启动 ===\n")
                self.store.update_side(job_id, side_name, {"attempts": attempt})
                for _ in range(wait):
                    if key in self._abort:
                        break
                    time.sleep(1)
                if key in self._abort:
                    break
                # Fresh copy from the baseline so a retry cannot mix two sessions'
                # edits into one product (single-turn evidence integrity).
                try:
                    self._fresh_workspace(job_id, side_name)
                except Exception as exc:
                    # Never launch into the discarded turn's dirty workspace:
                    # skip this attempt and try another re-copy after backoff.
                    msg = (f"#{attempt}/{max_attempts} 重新复制干净工作区失败：{exc}"
                           "（不在脏副本里启动，直接进入下一次重试）")
                    with log_path.open("a", encoding="utf-8") as f:
                        f.write(f"[retry] {msg}\n")
                    attempts_log.append(msg)
                    continue
            # Mark running BEFORE launch: a single attempt may run for up to
            # side_timeout_seconds and _fresh_workspace just reset status to
            # pending, otherwise the UI wrongly shows "待运行" mid-run and
            # crash recovery would miss the side.
            self.store.update_side(job_id, side_name, {"status": "running"})
            result = self._run_attempt(
                job_id, side_name, wdir, evidence_dir, log_path,
                timeout=timeout, stall_after=stall_after, poll_every=poll_every,
                attempt=attempt,
            )
            self.store.update_side(job_id, side_name, {"status": "running", "attempts": attempt})
            attempts_log.append(result["summary"])
            if result["aborted"]:
                break
            if result["completed"]:
                break

        result = result or {"code": -1, "new_session": None, "completed": False,
                            "aborted": False, "summary": "未执行"}
        aborted = key in self._abort
        if aborted:
            self._abort.discard(key)

        patch = {
            "exit_code": result["code"],
            "finished_at": _stamp(),
            "duration_seconds": int(time.time() - started),
            "attempts_log": attempts_log,
        }

        completed = bool(result.get("completed"))
        chosen = None if aborted else (result.get("new_session") if completed else None)
        if chosen:
            kept = evidence_dir / f"{side_name.lower()}-{chosen['session_id']}.jsonl"
            shutil.copyfile(chosen["path"], kept)
            patch["session_id"] = chosen["session_id"]
            patch["jsonl_local"] = str(kept)

        if not aborted and completed and chosen:
            try:
                fin = ws.finalize_side(wdir, side["branch"], f"Pair {job_id} side {side_name} product",
                                       force=bool(side.get("retry_of")))
                patch["head_sha"] = fin["sha"]
                patch["head_url"] = fin["url"]
                patch["pushed"] = True
            except ws.GitError as exc:
                patch["error"] = f"产物提交/推送失败: {exc}"
        elif not aborted:
            # Incomplete rounds are discarded by the fresh-copy retry; a partial
            # commit on the branch would be force-overwritten by the next attempt.
            pass

        job = self.store.get_job(job_id)
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
        elif not completed:
            last_failure = result.get("failure") or (
                "达到总超时" if result["code"] == 124 else
                f"退出码={result['code']}（多为网关 504/断流）" if result["code"] not in (0, None)
                else "未见 result:success")
            tries = len(attempts_log)
            patch["status"] = "failed"
            patch["error"] = (
                patch.get("error", "")
                + f" {last_failure}；已尝试 {tries} 次（上限 {max_attempts}，每次断流都会换干净基线副本重跑）仍未拿到完整轮次，见 {log_path.name}"
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
                    if "API Error:" in line and "504" in line:
                        signal["api_error_504"] = True
                    try:
                        ev = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    etype = ev.get("type")
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
        except Exception:
            pass
        finally:
            done.set()

    def _run_attempt(
        self, job_id: str, side_name: str, wdir: str, evidence_dir: Path, log_path: Path,
        *, timeout: int, stall_after: int, poll_every: int, attempt: int,
    ) -> dict:
        """Launch claude once under a silent-stall watchdog; return attempt result."""
        from . import procmon

        settings = self.store.settings()
        job = self.store.get_job(job_id)
        prompt = job["prompt"]
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in (settings.get("env_overrides") or {}).items()})
        command = settings.get("claude_command", "claude")
        # bypassPermissions: -p 非交互下 acceptEdits 只放行文件编辑，Bash 会全部
        # 自动拒绝（pair-34679bd1de A 侧 35 次 python/pytest 被拦，产物零验证）。
        # 工作区本就是一次性基线副本，可整体丢弃，放行全部工具。
        permission_mode = settings.get("permission_mode", "bypassPermissions")
        args = [command, "-p", "--permission-mode", permission_mode,
                "--output-format", "stream-json", "--include-partial-messages",
                "--verbose"]
        stdin_data = None
        if len(prompt) < 1500:
            args.append(prompt)
        else:
            stdin_data = prompt

        key = f"{job_id}/{side_name}"
        baseline_mtime = self._latest_transcript_mtime(wdir)
        t0 = time.time()
        stalls = 0
        killed_reason = ""

        with log_path.open("a", encoding="utf-8") as logf:
            if attempt == 1:
                logf.write(f"$ claude -p --permission-mode {permission_mode} --output-format stream-json  "
                           f"[prompt via {'argv' if stdin_data is None else 'stdin'}]\n\n")
            logf.flush()
            raw_stream = evidence_dir / f"{side_name.lower()}-stream.jsonl"
            proc = subprocess.Popen(
                args, cwd=wdir, env=env,
                stdin=subprocess.PIPE if stdin_data is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1,
            )
            pump_done = threading.Event()
            signal: dict = {}
            pump = threading.Thread(
                target=self._pump_stream,
                args=(proc, logf, raw_stream, pump_done, signal),
                daemon=True,
            )
            pump.start()
            with self._lock:
                self._procs[key] = proc
            reg = self._reg_path(job_id, side_name)
            reg.write_text(json.dumps({
                "job_id": job_id, "side": side_name, "pid": proc.pid,
                "workspace": wdir, "started": _stamp(),
            }, ensure_ascii=False), encoding="utf-8")
            prev_snap = procmon.snapshot()
            idle_for = 0.0
            code = None
            try:
                while True:
                    if proc.poll() is not None:
                        code = proc.returncode
                        break
                    for _ in range(poll_every):
                        if proc.poll() is not None or key in self._abort:
                            break
                        time.sleep(1)
                    if proc.poll() is not None:
                        code = proc.returncode
                        break
                    if key in self._abort:
                        procmon.kill_tree(proc.pid)
                        proc.wait(timeout=30)
                        killed_reason = "manual-abort"
                        break
                    if time.time() - t0 > timeout:
                        procmon.kill_tree(proc.pid)
                        proc.wait(timeout=30)
                        killed_reason = "total-timeout"
                        break
                    cur_snap = procmon.snapshot()
                    tree = procmon.descendants(proc.pid, cur_snap)
                    cpu_io_busy = procmon.tree_busy(prev_snap, cur_snap, tree, proc.pid)
                    new_transcript = self._latest_transcript_mtime(wdir) > baseline_mtime + 1
                    idle_for = 0.0 if (cpu_io_busy or new_transcript) else idle_for + poll_every
                    prev_snap = cur_snap
                    if idle_for >= stall_after:
                        stalls += 1
                        logf.write(f"\n[watchdog] {int(idle_for)}s 无 CPU/IO/轨迹活动，判定网关断流，"
                                   "终止本次尝试并自动重试\n")
                        logf.flush()
                        procmon.kill_tree(proc.pid)
                        proc.wait(timeout=30)
                        killed_reason = "stall"
                        break
            finally:
                pump_done.wait(timeout=15)  # let the reader drain stdout before logf closes
                with self._lock:
                    self._procs.pop(key, None)
                try:
                    reg.unlink()
                except OSError:
                    pass
        duration = int(time.time() - t0)
        if killed_reason == "total-timeout":
            code = 124
        elif code is None:
            code = -1
        # Pick the newest fresh transcript that is genuinely complete. A session
        # whose tail is a gateway API Error or a dangling tool_result is a cut
        # turn and can never be accepted, regardless of the CLI's exit code or
        # of a trailing result:success event.
        candidates = [
            s for s in ws.find_session_jsonl(wdir)
            if float(s["mtime"]) >= baseline_mtime - 1
        ] if killed_reason != "manual-abort" else []
        new_session = None
        cutoff_reason = ""
        for s in candidates:
            reason = ws.transcript_interruption_reason(s["path"])
            if reason is None:
                new_session = s
                break
            # candidates are newest first; remember the newest cutoff reason.
            if not cutoff_reason:
                cutoff_reason = reason
        # The CLI emits subtype=success even for a fatal API error, distinguished
        # by is_error=true; and an error-during-run leaves no usable result at
        # all. Only a clean result together with a complete transcript counts.
        result_ok = signal.get("result") == "success" and not signal.get("result_is_error")
        completed = result_ok and bool(new_session)
        failure = "" if completed else PairRunner._classify_attempt(
            killed_reason, code, signal, cutoff_reason, bool(candidates))
        summary = (f"#{attempt} exit={code} {duration}s stalls={stalls} "
                   f"{'完成' if completed else ('有会话未完成' if new_session else '无新会话')}"
                   + (f" [原因: {failure}]" if failure else ""))
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n[attempt] {summary} killed={killed_reason or '-'} "
                       f"result={signal.get('result', '-')} is_error={signal.get('result_is_error', False)} "
                       f"504={signal.get('api_error_504', False)}\n")
        return {
            "code": code, "new_session": new_session, "completed": completed,
            "duration": duration, "failure": failure,
            "stalls": stalls, "aborted": killed_reason == "manual-abort",
            "summary": summary,
        }

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
        if signal.get("result_is_error"):
            return f"CLI 上报 result 错误（subtype={signal.get('result', '?')}）"
        if code not in (0, None):
            return f"claude 退出码 {code}（多为网关错误）"
        if not has_candidates:
            return "未产生新会话"
        return "未见 result:success"

    @staticmethod
    def _latest_transcript_mtime(wdir: str) -> float:
        from . import procmon
        config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
        return procmon.newest_jsonl_mtime(config_dir / "projects", ws._encode_cwd(wdir))


    def _fresh_workspace(self, job_id: str, side_name: str) -> dict:
        """Delete one side workspace and re-copy it from the baseline (retry)."""
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "") or str(
            self.store.workspace_pair_dir(job_id) / side_name.lower())
        if Path(wdir).exists():
            ws.robust_rmtree(wdir)
        fresh = ws.prepare_side_workspace(
            job["baseline_repo"], wdir, side["branch"], job.get("copy_excludes", ""),
        )
        self.store.update_side(job_id, side_name, {
            "workspace": fresh["workspace"], "status": "pending", "error": "",
            "head_sha": "", "head_url": "", "session_id": "", "jsonl_local": "",
            "trace_url": "", "pushed": False, "exit_code": None,
            "retry_of": side.get("head_sha") or "1",
        })
        return fresh

    def abort_side(self, job_id: str, side_name: str) -> None:
        """Request termination of a side.

        Works both while the CLI is running (terminate the process tree via the
        watchdog loop) and while the side is sleeping in retry backoff (the
        1-second backoff loop consumes the flag). A flag set while nothing is
        running is harmless: run_side discards it at the start of every attempt.
        """
        key = f"{job_id}/{side_name}"
        with self._lock:
            proc = self._procs.get(key)
            self._abort.add(key)
            if proc is not None:
                proc.terminate()

    def retry_side(self, job_id: str, side_name: str) -> None:
        if self.is_side_running(job_id, side_name):
            raise RuntimeError(
                f"{side_name} 侧正在运行，不能重跑。请先点「中止」等它结束（状态变成失败/完成）后再重跑。"
            )
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "") or str(self.store.workspace_pair_dir(job_id) / side_name.lower())
        self._fresh_workspace(job_id, side_name)
        # Keep the job tag truthful: a sibling side may already be running.
        self._sync_job_status(job_id)
        self.enqueue(job_id)

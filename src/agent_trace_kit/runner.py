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
        running_dir = self.store.home / "running"
        if running_dir.is_dir():
            for reg in running_dir.glob("*.json"):
                try:
                    info = json.loads(reg.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    info = {}
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
                if side["status"] in ("preparing", "running", "collecting"):
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
        max_attempts = max(1, int(settings.get("side_max_attempts", 6)))
        stall_after = max(30, int(settings.get("stall_seconds", 240)))
        poll_every = max(5, int(settings.get("activity_poll_seconds", 15)))
        timeout = int(settings.get("side_timeout_seconds", 1800))

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
                wait = min(30, 3 * attempt)
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
                    with log_path.open("a", encoding="utf-8") as f:
                        f.write(f"[retry] 重新复制工作区失败：{exc}\n")
            result = self._run_attempt(
                job_id, side_name, wdir, evidence_dir, log_path,
                timeout=timeout, stall_after=stall_after, poll_every=poll_every,
                attempt=attempt,
            )
            attempts_log.append(result["summary"])
            if result["aborted"]:
                break
            if result["code"] == 0 and result["new_session"]:
                break

        result = result or {"code": -1, "new_session": None, "duration": 0,
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

        chosen = None if aborted else result.get("new_session")
        if chosen:
            kept = evidence_dir / f"{side_name.lower()}-{chosen['session_id']}.jsonl"
            shutil.copyfile(chosen["path"], kept)
            patch["session_id"] = chosen["session_id"]
            patch["jsonl_local"] = str(kept)

        if not aborted and result["code"] == 0 and chosen:
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
        elif result["code"] != 0:
            why = "达到总超时" if result["code"] == 124 else f"claude 退出码={result['code']}（多为断流）"
            tries = f"（已自动重试 {len(attempts_log)} 次仍未完成）" if len(attempts_log) > 1 else ""
            patch["status"] = "failed"
            patch["error"] = (patch.get("error", "") + f" {why}{tries}，见 {log_path.name}").strip()
        elif not patch.get("session_id"):
            patch["status"] = "failed"
            patch["error"] = f"重试 {len(attempts_log)} 次后仍未产生完整的首轮会话（可能被断流截断），见 {log_path.name}"
        else:
            patch["status"] = "done"
        self.store.update_side(job_id, side_name, patch)

    @staticmethod
    def _pump_stream(proc: subprocess.Popen, logf, raw_path: Path, done: threading.Event) -> None:
        """Read claude stream-json stdout: raw copy + a readable progress trace."""
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
                    try:
                        ev = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    etype = ev.get("type")
                    if etype == "result":
                        emit(f"[result] {ev.get('subtype')} turns={ev.get('num_turns')} "
                             f"cost=${ev.get('total_cost_usd')}\n{str(ev.get('result', ''))[:3000]}")
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
        args = [command, "-p", "--permission-mode", "acceptEdits",
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
                logf.write("$ claude -p --permission-mode acceptEdits --output-format stream-json  "
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
            pump = threading.Thread(
                target=self._pump_stream,
                args=(proc, logf, raw_stream, pump_done),
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
        sessions = [] if killed_reason == "manual-abort" else [
            s for s in ws.find_session_jsonl(wdir)
            if float(s["mtime"]) >= baseline_mtime - 1
            and ws.session_has_assistant(s["path"])
            and ws.session_turn_complete(s["path"])
        ]
        new_session = sessions[0] if sessions else None
        summary = f"#{attempt} exit={code} {duration}s stalls={stalls} {'有新会话' if new_session else '无新会话'}"
        with log_path.open("a", encoding="utf-8") as logf:
            logf.write(f"\n[attempt] {summary} killed={killed_reason or '-'}\n")
        return {
            "code": code, "new_session": new_session, "duration": duration,
            "stalls": stalls, "aborted": killed_reason == "manual-abort",
            "summary": summary,
        }

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
        """Terminate a running side; it lands as failed and can be re-copied/retried."""
        with self._lock:
            proc = self._procs.get(f"{job_id}/{side_name}")
            if proc is not None:
                self._abort.add(f"{job_id}/{side_name}")
                proc.terminate()
            else:
                raise RuntimeError(f"{side_name} 侧当前没有运行中的进程")

    def retry_side(self, job_id: str, side_name: str) -> None:
        if self.is_side_running(job_id, side_name):
            raise RuntimeError(
                f"{side_name} 侧正在运行，不能重跑。请先点「中止」等它结束（状态变成失败/完成）后再重跑。"
            )
        job = self.store.get_job(job_id)
        side = job["sides"][side_name]
        wdir = side.get("workspace", "") or str(self.store.workspace_pair_dir(job_id) / side_name.lower())
        self._fresh_workspace(job_id, side_name)
        self.store.update_job(job_id, {"status": "ready"})
        self.enqueue(job_id)

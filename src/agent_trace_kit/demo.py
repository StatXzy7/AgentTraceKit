"""Sequential, server-side product demonstrations; never makes human GSB claims.

Runs a disposable copy of the committed artifact on an X11 display. FFmpeg
captures that display, including the visible terminal/browser, rather than
synthesizing video frames. Recipes are argv lists, not shell strings.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath

from . import procmon
from .desk_store import DeskStore, utc_now
from .ports import find_free_port, list_listeners, probe_http
from .recorder import Recorder


def argv(value) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ValueError("演示命令必须是非空 argv 数组")
    if any(not isinstance(v, str) or "\x00" in v for v in value):
        raise ValueError("演示命令的每个参数必须是字符串")
    return value


def validate_recipe(recipe: dict) -> dict:
    """Validate and detach a finite, argv-based demonstration recipe."""
    if not isinstance(recipe, dict):
        raise ValueError("演示配方必须是 JSON 对象")
    try:
        raw = json.dumps(recipe, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("演示配方必须包含有效 JSON 值") from exc
    if len(raw.encode("utf-8")) > 128_000:
        raise ValueError("演示配方超过 128 KB")
    recipe = json.loads(raw)
    if recipe.get("kind") not in ("web", "terminal"):
        raise ValueError("演示 kind 必须为 web 或 terminal")
    argv(recipe.get("start"))
    setup = recipe.get("setup", [])
    if not isinstance(setup, list) or len(setup) > 8:
        raise ValueError("setup 最多包含 8 条命令")
    for command in setup:
        argv(command)
    path = recipe.get("path", "/")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "\\" in path:
        raise ValueError("path 必须是本站路径，例如 /，不能是外部网址")
    steps = recipe.get("steps", [])
    if not isinstance(steps, list) or len(steps) > 30:
        raise ValueError("steps 最多包含 30 步")
    for step in steps:
        if not isinstance(step, dict) or step.get("action") not in {"click", "fill", "press", "scroll", "wait", "assert_text", "assert_visible"}:
            raise ValueError("演示步骤 action 不受支持")
    return recipe


def recipe_for(root: Path) -> dict:
    recipe_path = root / "atk-demo.json"
    if recipe_path.is_file():
        raw = recipe_path.read_text(encoding="utf-8")
        if len(raw) > 128_000:
            raise ValueError("atk-demo.json 超过 128 KB")
        recipe = json.loads(raw)
        recipe["source"] = "atk-demo.json"
    elif (root / "package.json").is_file():
        pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
        scripts = pkg.get("scripts", {})
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        setup = [["npm", "ci" if (root / "package-lock.json").exists() else "install", "--no-audit", "--no-fund"]]
        if "vite" in deps and "dev" in scripts:
            start = ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "{port}", "--strictPort"]
        elif "next" in deps and "dev" in scripts:
            start = ["npm", "run", "dev", "--", "--hostname", "127.0.0.1", "--port", "{port}"]
        else:
            raise ValueError("未识别 Node 启动方式，请在产物提交中提供 atk-demo.json")
        recipe = {"kind": "web", "setup": setup, "start": start, "source": "package.json", "steps": []}
    elif (root / "index.html").is_file() or (root / "public/index.html").is_file():
        folder = "." if (root / "index.html").is_file() else "public"
        recipe = {"kind": "web", "start": [sys.executable, "-m", "http.server", "{port}", "--bind", "127.0.0.1", "--directory", folder],
                  "source": "static-html", "steps": []}
    else:
        raise ValueError("未识别产物入口，请在产物提交中提供 atk-demo.json（web 或 terminal）")
    return validate_recipe(recipe)


def artifact_key(side: dict) -> str:
    values = [side.get(k, "") for k in ("head_sha", "session_id", "finished_at")]
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()[:24]


def _extra_files(value: dict | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 8:
        raise ValueError("extra_files 必须是最多包含 8 个文件的对象")
    total = 0
    for name, content in value.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise ValueError("extra_files 路径和内容必须是 UTF-8 字符串")
        path = PurePosixPath(name)
        if (not name.startswith(".atk-review/") or "\\" in name or ":" in name
                or "\x00" in name or path.is_absolute() or len(path.parts) < 2
                or any(part in ("", ".", "..") for part in name.split("/"))):
            raise ValueError("extra_files 只能写入 .atk-review/ 内的相对路径")
        try:
            size = len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("extra_files 内容不是有效 UTF-8") from exc
        total += size
        if size > 64_000 or total > 128_000:
            raise ValueError("extra_files 单文件最多 64 KB，总计最多 128 KB")
    return dict(value)


def _content_hash(value: dict) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _write_extra_files(work: Path, files: dict[str, str]) -> None:
    """Write only new helper files inside the disposable artifact copy."""
    root = work.resolve()
    # Validate the entire set before creating any file.
    for name in files:
        target = root / name
        if not target.resolve().is_relative_to(root) or target.exists() or target.is_symlink():
            raise ValueError("extra_files 拒绝路径逃逸或覆盖产物文件")
        for parent in target.parents:
            if parent == root:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError("extra_files 拒绝符号链接或非目录父路径")
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("x", encoding="utf-8", newline="\n") as output:
            output.write(content)


class DemoManager:
    def __init__(self, store: DeskStore, recorder: Recorder):
        self.store, self.recorder = store, recorder
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._active: tuple[str, str] | None = None
        self.last_error = ""

    def busy(self, job_id: str) -> bool:
        with self._lock:
            if self._active and self._active[0] == job_id:
                return True
            job = self.store.get_job(job_id)
            return any(s.get("demo", {}).get("status") in ("queued", "running") for s in job["sides"].values())

    def pair_may_start(self) -> bool:
        """Give finished products the display/CPU before starting the next pair."""
        if not sys.platform.startswith("linux"):
            return True
        if self._active:
            return False
        auto = self.store.settings().get("linux_auto_demo", False)
        for job in self.store.list_jobs():
            if job.get("archived") or self.store.review_locked(job):
                continue
            if any(s["status"] not in ("done", "failed") for s in job["sides"].values()):
                continue
            for side in job["sides"].values():
                info = side.get("demo", {})
                if info.get("status") in ("queued", "running"):
                    return False
                if auto and side["status"] == "done" and info.get("artifact_key") != artifact_key(side):
                    return False
        return True

    def request(self, job_id: str, side: str, recipe: dict | None = None,
                expected_artifact_key: str | None = None,
                extra_files: dict[str, str] | None = None) -> dict:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("自动演示在 Linux 服务器运行")
        with self._lock:
            job = self.store.get_job(job_id)
            if side not in ("A", "B"):
                raise ValueError("side 必须为 A 或 B")
            if self.busy(job_id) or self.store.review_locked(job) or job.get("archived"):
                raise RuntimeError("任务正在演示、已锁定或已归档")
            if job["sides"][side]["status"] not in ("done", "failed"):
                raise RuntimeError("请等待产物运行结束后再演示")
            current = job["sides"][side]
            key = artifact_key(current)
            if expected_artifact_key is not None and expected_artifact_key != key:
                raise ValueError("演示配方绑定的产物版本已经改变")
            if recipe is None and extra_files is not None:
                raise ValueError("extra_files 必须和外部演示配方一起提交")
            info = {"status": "queued", "artifact_key": key, "requested_at": utc_now()}
            if recipe is not None:
                if expected_artifact_key is None:
                    raise ValueError("外部演示配方必须提供 expected_artifact_key")
                recipe = validate_recipe(recipe)
                recipe["source"] = "operator-authorized-external"
                files = _extra_files(extra_files)
                envelope = {"schema": 1, "job": job_id, "side": side,
                            "artifact_key": key,
                            "artifact": {k: current.get(k, "") for k in ("head_sha", "session_id", "finished_at")},
                            "provenance": "operator-authorized-automated-recipe",
                            "requested_at": info["requested_at"], "recipe": recipe, "extra_files": files}
                digest = _content_hash(envelope)
                relative = Path("demo-overrides") / side.lower() / f"{uuid.uuid4().hex}.json"
                self.store._atomic_write_json(self.store.evidence_dir(job_id) / relative, envelope)
                info["recipe_override"] = {"path": relative.as_posix(), "sha256": digest,
                                           "artifact_key": key, "provenance": envelope["provenance"]}
            self.store.update_side(job_id, side, {"demo": info})
            return info

    def _recipe_for_run(self, job: dict, side: str, info: dict, work: Path) -> tuple[dict, dict]:
        override = info.get("recipe_override")
        if override is None:
            return recipe_for(work), {"source": "committed-artifact-or-autodetection"}
        if not isinstance(override, dict):
            raise ValueError("外部演示配方证据格式无效")
        evidence = self.store.evidence_dir(job["id"]).resolve()
        relative = override.get("path", "")
        if (not isinstance(relative, str) or not relative.startswith(f"demo-overrides/{side.lower()}/")
                or "\\" in relative or ":" in relative
                or any(part in ("", ".", "..") for part in relative.split("/"))):
            raise ValueError("外部演示配方证据路径无效")
        path = evidence / relative
        if not path.resolve().is_relative_to(evidence) or path.is_symlink():
            raise ValueError("外部演示配方证据路径逃逸")
        # JSON escapes can expand a bounded UTF-8 helper (for example tabs)
        # beyond its decoded size; keep a separate bounded envelope limit.
        if path.stat().st_size > 1_000_000:
            raise ValueError("外部演示配方证据超过大小限制")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        key = artifact_key(job["sides"][side])
        binding = {k: job["sides"][side].get(k, "") for k in ("head_sha", "session_id", "finished_at")}
        if (envelope.get("schema") != 1 or envelope.get("job") != job["id"] or envelope.get("side") != side
                or envelope.get("artifact_key") != key or override.get("artifact_key") != key
                or envelope.get("artifact") != binding):
            raise ValueError("外部演示配方绑定的产物版本已经改变")
        digest = _content_hash(envelope)
        if digest != override.get("sha256"):
            raise ValueError("外部演示配方证据 SHA256 不匹配")
        recipe = validate_recipe(envelope.get("recipe"))
        files = _extra_files(envelope.get("extra_files"))
        _write_extra_files(work, files)
        return recipe, {"source": "operator-authorized-external", "sha256": digest,
                        "artifact_key": key, "artifact": binding,
                        "provenance": "operator-authorized-automated-recipe",
                        "path": relative, "extra_files": sorted(files)}

    def start(self) -> None:
        if not sys.platform.startswith("linux") or self._thread:
            return
        for job in self.store.list_jobs():
            for name, side in job["sides"].items():
                if side.get("demo", {}).get("status") == "running":
                    info = {**side["demo"], "status": "failed", "error": "服务中断；保留已写证据，请手动重试演示", "finished_at": utc_now()}
                    self.store.update_side(job["id"], name, {"demo": info})
        self._thread = threading.Thread(target=self._loop, name="linux-demo", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=20)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for job in reversed(self.store.list_jobs()):
                    if self._stop.is_set():
                        break
                    if job.get("archived") or self.store.review_locked(job):
                        continue
                    # Wait for both CLI sides so recording gets the small host's CPU.
                    if any(s["status"] not in ("done", "failed") for s in job["sides"].values()):
                        continue
                    for name, side in job["sides"].items():
                        info = side.get("demo", {})
                        auto = self.store.settings().get("linux_auto_demo", False) and side["status"] == "done"
                        if info.get("status") == "queued" or (auto and info.get("artifact_key") != artifact_key(side)):
                            self.run_one(job["id"], name)
                self.last_error = ""
            except Exception as exc:
                self.last_error = str(exc)
                print(f"[linux-demo] {type(exc).__name__}: {exc}", flush=True)
            self._stop.wait(3)

    def _wait(self, seconds: float) -> None:
        if self._stop.wait(seconds):
            raise RuntimeError("服务正在停止")

    def _command(self, command: list[str], cwd: Path, log: Path, timeout: float, env: dict) -> None:
        with log.open("a", encoding="utf-8") as output:
            output.write("\n$ " + json.dumps(command, ensure_ascii=False) + "\n")
            output.flush()
            proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=output, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + timeout
            try:
                while proc.poll() is None:
                    if time.monotonic() > deadline:
                        raise RuntimeError(f"命令超时（{timeout}s）：{command[0]}")
                    self._wait(0.2)
                if proc.returncode:
                    raise RuntimeError(f"命令退出 {proc.returncode}，查看 setup.log")
            finally:
                procmon.kill_tree(proc.pid)
                proc.wait(timeout=10)

    def run_one(self, job_id: str, side: str) -> dict:
        with self._lock:
            if self._active:
                raise RuntimeError("另一项 Linux 演示正在进行")
            job = self.store.get_job(job_id)
            if self.store.review_locked(job) or job.get("archived"):
                raise RuntimeError("任务已锁定或已归档")
            if any(s["status"] not in ("done", "failed") for s in job["sides"].values()):
                raise RuntimeError("请等待 A/B 产物运行完成")
            self._active = (job_id, side)
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        folder = self.store.evidence_dir(job_id) / "demo" / side.lower() / run_id
        folder.mkdir(parents=True)
        report = {"status": "running", "artifact_key": artifact_key(job["sides"][side]),
                  "job": job_id, "side": side, "run_id": run_id, "started_at": utc_now(),
                  "provenance": "automated-linux-x11", "human_reviewed": False,
                  "report": str(folder / "report.json"), "steps": [], "error": ""}
        request_info = job["sides"][side].get("demo", {})
        if "recipe_override" in request_info:
            report["recipe_override"] = request_info["recipe_override"]
        self.store.update_side(job_id, side, {"demo": report})
        product = None
        recording = False
        logf = None
        try:
            if not os.environ.get("DISPLAY"):
                raise RuntimeError("DISPLAY 未配置，请启动 agenttracekit-desktop")
            source = Path(job["sides"][side]["workspace"]).resolve()
            sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True, encoding="utf-8").strip()
            expected = job["sides"][side].get("head_sha")
            if expected and sha != expected:
                raise RuntimeError("工作区 HEAD 已改变；拒绝把不同版本的视频绑定到原轨迹")
            report["head_sha"] = sha
            work = self.store.home / "demo-workspaces" / job_id / side.lower() / run_id
            work.mkdir(parents=True)
            archive = work.parent / f"{run_id}.tar"
            with archive.open("wb") as output:
                subprocess.run(["git", "archive", "--format=tar", sha], cwd=source, stdout=output,
                               stderr=subprocess.PIPE, check=True, timeout=60)
            with tarfile.open(archive) as tar:
                tar.extractall(work, filter="data")
            archive.unlink()
            recipe, recipe_evidence = self._recipe_for_run(job, side, request_info, work)
            report["recipe"] = recipe
            report["recipe_evidence"] = recipe_evidence
            report["workspace"] = str(work)
            self.store._atomic_write_json(folder / "recipe.json", recipe)
            port = find_free_port(skip={8765, 5900, 6080})
            env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1", "PYTHONUNBUFFERED": "1"}
            for command in recipe.get("setup", []):
                expanded = [v.replace("{port}", str(port)).replace("{python}", sys.executable) for v in command]
                self._command(expanded, work, folder / "setup.log", 300, env)
            command = [v.replace("{port}", str(port)).replace("{python}", sys.executable) for v in recipe["start"]]
            settings = self.store.settings()
            cap = min(89, max(30, int(settings.get("video_max_seconds", 89))))
            spec = {"command": command, "cwd": str(work), "result": str(folder / "process.json"),
                    "timeout": cap - 25, "presentation_timeout": cap - 10,
                    "readable": recipe["kind"] == "terminal",
                    "chars_per_second": settings.get("demo_output_chars_per_second", 40),
                    "command_pause": settings.get("demo_command_pause_seconds", 5),
                    "page_pause": settings.get("demo_page_pause_seconds", 6),
                    "final_pause": settings.get("demo_final_pause_seconds", 12),
                    "min_seconds": min(cap - 12, settings.get("demo_min_seconds", 30))}
            report["presentation"] = {k: v for k, v in spec.items() if k not in ("command", "cwd", "result")}
            self.store._atomic_write_json(folder / "process-spec.json", spec)
            self.recorder.start(job_id, side, fps=10, max_seconds=cap)
            recording = True
            self._wait(1)
            if not self.recorder.status(job_id, side).get("recording"):
                raise RuntimeError(self.recorder.status(job_id, side).get("error") or "录屏过早结束")
            logf = (folder / "product.log").open("w", encoding="utf-8")
            product = subprocess.Popen(["xterm", "-geometry", "110x28+0+0", "-fa", "DejaVu Sans Mono", "-fs", "14",
                                        "-bg", "#10141c", "-fg", "#e5e7eb",
                                        "-title", f"ATK {job_id} {side} | automated demo", "-e", sys.executable,
                                        "-m", "agent_trace_kit.demo", "terminal", str(folder / "process-spec.json")],
                                       cwd=work, env=env, stdin=subprocess.DEVNULL, stdout=logf,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + cap - 5
            if recipe["kind"] == "web":
                url = f"http://127.0.0.1:{port}" + recipe.get("path", "/")
                while not probe_http(url, timeout=0.6).get("ok"):
                    if product.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("产物未在录屏期限内启动，查看 product.log / process.json")
                    self._wait(0.5)
                # Verify the listener belongs to this launch, never reuse another app.
                rows = [r for r in list_listeners() if r["port"] == port]
                if not rows or any(not r.get("pid") or os.getpgid(r["pid"]) != os.getpgid(product.pid) for r in rows):
                    # xterm gives its child a fresh session; check ancestry too.
                    snap = procmon.snapshot()
                    def owned(pid):
                        seen = set()
                        while pid and pid not in seen:
                            if pid == product.pid:
                                return True
                            seen.add(pid)
                            pid = snap.get(pid, {}).get("ppid", 0)
                        return False
                    if not rows or any(not owned(r.get("pid", 0)) for r in rows):
                        raise RuntimeError("监听端口不属于本次产物，拒绝录制其他服务")
                self._browser(url, recipe.get("steps", []), folder, report, deadline)
            else:
                while not (folder / "process.json").exists():
                    if product.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("终端演示未完成，查看 product.log")
                    self._wait(0.25)
                result = json.loads((folder / "process.json").read_text(encoding="utf-8"))
                report["process"] = result
                if not result.get("presentation_complete", False):
                    raise RuntimeError(result.get("error") or "终端输出尚未完整展示")
                if result.get("timed_out"):
                    raise RuntimeError("演示命令超时（失败过程已录制）")
                if result["exit_code"] != 0:
                    raise RuntimeError(f"产物退出 {result['exit_code']}（失败过程已录制）")
                self._wait(1)
            if not self.recorder.status(job_id, side).get("recording"):
                raise RuntimeError("录屏提前结束，未完整覆盖演示")
            report["status"] = "ready"
        except Exception as exc:
            report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            if recording:
                try:
                    video = self.recorder.stop(job_id, side)
                    report["video"] = video
                    if video.get("error"):
                        report.update(status="failed", error=video["error"])
                    elif video.get("path"):
                        shutil.copyfile(video["path"], folder / "video.mp4")
                        digest = hashlib.sha256()
                        with (folder / "video.mp4").open("rb") as f:
                            for block in iter(lambda: f.read(1024 * 1024), b""):
                                digest.update(block)
                        report["video_sha256"] = digest.hexdigest()
                except Exception as exc:
                    report.update(status="failed", error=f"录屏收尾失败: {exc}")
            if product:
                # xterm launches a session leader: terminate its tracked descendants
                # before the terminal to avoid leaving a server after demo exit.
                try:
                    self._kill_product(product)
                except Exception as exc:
                    report.update(status="failed", error=f"产物清理失败: {exc}")
            if logf:
                logf.close()
            report["finished_at"] = utc_now()
            try:
                self.store._atomic_write_json(folder / "report.json", report)
                self.store.update_side(job_id, side, {"demo": report})
            finally:
                with self._lock:
                    self._active = None
        return report

    @staticmethod
    def _kill_product(proc):
        snap = procmon.snapshot()
        children = [pid for pid, row in snap.items() if row.get("ppid") == proc.pid]
        for pid in children:
            procmon.kill_tree(pid)
        procmon.kill_tree(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _browser(self, url: str, steps: list, folder: Path, report: dict, deadline: float) -> None:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            with pw.chromium.launch(headless=False, chromium_sandbox=True,
                                    args=["--window-size=1280,720", "--window-position=0,0", "--disable-dev-shm-usage"]) as browser:
                page = browser.new_page(viewport={"width": 1260, "height": 600})
                errors = []
                page.on("pageerror", lambda exc: errors.append(str(exc)))
                page.goto(url, wait_until="domcontentloaded", timeout=max(1000, min(25000, (deadline-time.monotonic())*1000)))
                report["url"] = url
                report["title"] = page.title()
                self._wait(3)
                if not steps:
                    steps = [{"action": "scroll", "y": 480}, {"action": "wait", "seconds": 2}, {"action": "scroll", "y": -480}]
                    report["coverage"] = "页面加载与滚动；未配置功能断言"
                else:
                    report["coverage"] = "仅覆盖 recipe 中列出的操作与断言"
                try:
                    for step in steps:
                        remaining = deadline - time.monotonic()
                        if remaining < 1:
                            raise RuntimeError("演示步骤超出录制期限")
                        page.set_default_timeout(min(5000, remaining * 1000))
                        action = step["action"]
                        entry = {"step": step, "status": "running"}
                        report["steps"].append(entry)
                        try:
                            if action in ("click", "fill", "press", "assert_visible", "assert_text"):
                                loc = page.locator(step["selector"])
                                if action == "click": loc.click()
                                elif action == "fill": loc.fill(str(step.get("value", "")))
                                elif action == "press": loc.press(str(step["key"]))
                                elif action == "assert_visible": loc.wait_for(state="visible")
                                elif str(step["text"]) not in loc.inner_text(): raise AssertionError("页面文字断言失败")
                            elif action == "scroll":
                                distance = max(-1500, min(1500, int(step.get("y", 500))))
                                while distance:
                                    if time.monotonic() + 0.12 >= deadline:
                                        raise RuntimeError("滚动未完成，达到录屏时限")
                                    delta = max(-40, min(40, distance))
                                    page.mouse.wheel(0, delta)
                                    distance -= delta
                                    self._wait(0.12)
                            elif action == "wait": self._wait(min(5, remaining, max(0, float(step.get("seconds", 1)))))
                            entry["status"] = "passed"
                        except Exception as exc:
                            entry.update(status="failed", error=str(exc))
                            raise
                        self._wait(1.5)
                    self._wait(8)
                finally:
                    report["page_errors"] = errors
                    page.screenshot(path=str(folder / "last-frame.png"))
                if errors:
                    raise RuntimeError("浏览器有未处理异常，见 page_errors")


def terminal(spec_path: str) -> None:
    """Read the launch spec in the fresh, visible terminal helper process."""
    from .demo_terminal import terminal as present
    present(json.loads(Path(spec_path).read_text(encoding="utf-8")))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "terminal":
        terminal(sys.argv[2])

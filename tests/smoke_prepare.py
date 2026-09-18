"""End-to-end smoke of prepare pipeline against a local bare remote (no model calls)."""
import json
import subprocess
import sys
from pathlib import Path

from agent_trace_kit.checklist import run_checklist
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.runner import PairRunner

ROOT = Path(r"C:\AgentTraceKit-data\smoke-prepare")
import shutil
if ROOT.exists():
    shutil.rmtree(ROOT)
ROOT.mkdir(parents=True)

bare = ROOT / "remote.git"
subprocess.run(["git", "init", "--bare", str(bare)], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = ROOT / "task-repo"
base.mkdir()


def g(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


g(["init", "-b", "main"], base)
g(["config", "user.email", "t@example.com"], base)
g(["config", "user.name", "T"], base)
(base / ".gitignore").write_text(".env\n", encoding="utf-8")
(base / "PRD.md").write_text("# 初始题目仓库\n实现一个XX。\n", encoding="utf-8")
g(["add", "-A"], base)
g(["commit", "-m", "initial snapshot"], base)
g(["remote", "add", "origin", str(bare)], base)

store = DeskStore(ROOT / "desk")
runner = PairRunner(store)
job = store.create_job({
    "prompt": "请实现一个支持事件时间语义与故障恢复的流处理执行引擎",
    "task_type": "0-1代码生成",
    "difficulty": "地狱",
    "stack": "Go",
    "baseline_repo": str(base),
})
print("created:", job["id"])
info = runner.prepare(job["id"])
print("baseline sha:", info["baseline"]["sha"][:12], "pushed url:", info["baseline"]["url"])
print("sides:", info["sides"])

job = store.get_job(job["id"])
a_ws = Path(job["sides"]["A"]["workspace"])
b_ws = Path(job["sides"]["B"]["workspace"])
print("A workspace exists:", a_ws.is_dir(), "| branch:", job["sides"]["A"]["branch"])
print("B workspace exists:", b_ws.is_dir(), "| branch:", job["sides"]["B"]["branch"])
print("A has PRD.md:", (a_ws / "PRD.md").exists())
print("A/B branches differ:", job["sides"]["A"]["branch"] != job["sides"]["B"]["branch"])
print("job status:", job["status"])

report = run_checklist(job)
baseline_items = [x for x in report["items"] if x["id"].startswith("baseline")]
for x in baseline_items:
    print(f"  [{ 'OK' if x['ok'] else 'X ' }] {x['label']}: {x['detail']}")
print("blocking:", report["blocking_count"], "warnings:", report["warning_count"])
print("SMOKE OK")

"""Full re-verification: prepare real A/B workspaces from a baseline, check 1M injection + live call."""
import json
import shutil
import subprocess
from pathlib import Path

from agent_trace_kit import workspace as ws
from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.runner import PairRunner

ROOT = Path(r"C:\AgentTraceKit-data\_envrecheck")
if ROOT.exists():
    ws.robust_rmtree(ROOT)
ROOT.mkdir(parents=True)

bare = ROOT / "remote.git"
subprocess.run(["git", "init", "--bare", str(bare)], check=True,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
base = ROOT / "task"
base.mkdir()


def g(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


g(["init", "-b", "main"], base)
g(["config", "user.email", "t@example.com"], base)
g(["config", "user.name", "T"], base)
(base / ".gitignore").write_text(".env\n", encoding="utf-8")
(base / "README.md").write_text("# t\n", encoding="utf-8")
g(["add", "-A"], base)
g(["commit", "-m", "init"], base)
g(["remote", "add", "origin", str(bare)], base)

store = DeskStore(ROOT / "desk")
job = store.create_job({"prompt": "p", "baseline_repo": str(base)})
PairRunner(store).prepare(job["id"])
job = store.get_job(job["id"])

a = Path(job["sides"]["A"]["workspace"])
settings_file = a / ".claude" / "settings.local.json"
print("1) local settings exists:", settings_file.is_file())
print(json.dumps(json.loads(settings_file.read_text(encoding="utf-8")), ensure_ascii=False))

# verify it is git-ignored
chk = subprocess.run(["git", "check-ignore", ".claude/settings.local.json"],
                     cwd=a, capture_output=True, text=True)
print("2) git check-ignore:", chk.stdout.strip() or "NOT IGNORED")

# live call from inside the workspace (uses injected 1M config + inherited global gateway)
p = subprocess.run(
    ["claude", "--print", "reply with exactly: ok"],
    cwd=a, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
)
print("3) live call exit:", p.returncode, "| stdout tail:", p.stdout.strip().splitlines()[-1][:120])
print("   stderr:", (p.stderr or "").strip()[:200])

# confirm the produced session jsonl is discoverable by cwd
sessions = ws.find_session_jsonl(a)
print("4) discoverable sessions for workspace:", len(sessions), sessions[0]["session_id"] if sessions else "-")

ok = (
    settings_file.is_file()
    and chk.stdout.strip().endswith("settings.local.json")
    and p.returncode == 0
    and p.stdout.strip().endswith("ok")
    and len(sessions) >= 1
)
print("RESULT:", "ALL OK" if ok else "PROBLEM")

"""Run and package two local implementations under one frozen prompt.

The module deliberately uses only the Python standard library.  It keeps the
execution evidence (commands, exit codes and logs) next to the generated
dataset row so a later upload can be audited or repeated.
"""
from __future__ import annotations

import csv
import json
import os
import shlex
import subprocess
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATASET_COLUMNS = [
    "prompt", "evaluator", "generation_mode", "provider", "difficulty",
    "environment", "os", "dependencies", "baseline_commit",
    "A_directory", "A_commit", "A_session_id", "A_trace_url", "A_video_url",
    "B_directory", "B_commit", "B_session_id", "B_trace_url", "B_video_url",
    "winner", "label", "rationale", "A_check_status", "B_check_status",
    "created_at", "completed_at",
]


@dataclass
class PairSpec:
    prompt: str = ""
    evaluator: str = ""
    generation_mode: str = "0-1代码生成"
    provider: str = "Claude Code"
    difficulty: str = "困难"
    environment: str = ""
    os_name: str = "Windows"
    dependencies: str = "无外部依赖"
    baseline_commit: str = ""
    a_directory: str = ""
    b_directory: str = ""
    a_commit: str = ""
    b_commit: str = ""
    a_session_id: str = ""
    b_session_id: str = ""
    a_trace_url: str = ""
    b_trace_url: str = ""
    a_video_url: str = ""
    b_video_url: str = ""
    winner: str = ""
    label: str = ""
    rationale: str = ""
    commands: list[str] = field(default_factory=lambda: ["go version", "go build ./...", "go vet ./...", "go test ./..."])
    timeout_seconds: int = 900
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str = ""

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "PairSpec":
        names = {f.name for f in cls.__dataclass_fields__.values()}
        data = {k: v for k, v in value.items() if k in names}
        if "commands" in data and isinstance(data["commands"], str):
            data["commands"] = [x.strip() for x in data["commands"].splitlines() if x.strip()]
        return cls(**data)


def _git(directory: Path, *args: str) -> tuple[str, int]:
    try:
        p = subprocess.run(["git", *args], cwd=directory, text=True, encoding="utf-8", errors="replace",
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        return p.stdout.strip(), p.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc), 1


def _commit(directory: Path) -> str:
    out, code = _git(directory, "rev-parse", "HEAD")
    return out if code == 0 else ""


def validate_spec(spec: PairSpec, require_decision: bool = True) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    def check(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
    check("prompt", bool(spec.prompt.strip()), "提示词不能为空")
    check("A directory", bool(spec.a_directory) and Path(spec.a_directory).is_dir(), "A 目录存在且可读")
    check("B directory", bool(spec.b_directory) and Path(spec.b_directory).is_dir(), "B 目录存在且可读")
    check("different directories", bool(spec.a_directory and spec.b_directory and Path(spec.a_directory).resolve() != Path(spec.b_directory).resolve()), "A/B 必须是两个不同目录")
    for side, directory in (("A", spec.a_directory), ("B", spec.b_directory)):
        p = Path(directory) if directory else Path(".")
        check(f"{side} git repository", (p / ".git").exists(), f"{side} 目录包含 .git")
        check(f"{side} go.mod", (p / "go.mod").exists(), f"{side} 目录包含 go.mod（Go 项目）")
    check("environment", bool(spec.environment.strip()), "填写运行环境，例如 go version go1.26.0 linux/amd64")
    if require_decision:
        check("winner", spec.winner in ("A 更好", "Same", "B 更好"), "结论必须是 A 更好、Same 或 B 更好")
        check("rationale", bool(spec.rationale.strip()), "必须填写可核验的结论理由")
        for name, value, detail in (
            ("evaluator", spec.evaluator, "评测人"), ("generation_mode", spec.generation_mode, "生成模式"),
            ("provider", spec.provider, "Provider"), ("difficulty", spec.difficulty, "难度"),
            ("os", spec.os_name, "操作系统"), ("dependencies", spec.dependencies, "依赖说明"),
            ("baseline_commit", spec.baseline_commit, "基线提交"), ("A_session_id", spec.a_session_id, "A 会话 ID"),
            ("B_session_id", spec.b_session_id, "B 会话 ID"), ("A_trace_url", spec.a_trace_url, "A 轨迹 URL"),
            ("B_trace_url", spec.b_trace_url, "B 轨迹 URL"), ("A_video_url", spec.a_video_url, "A 视频 URL"),
            ("B_video_url", spec.b_video_url, "B 视频 URL")):
            check(name, bool(str(value).strip()), f"必须填写{detail}")
    check("commands", bool(spec.commands), "至少配置一条检查命令")
    return checks


def run_checks(directory: str | Path, commands: list[str], timeout: int = 900) -> dict[str, Any]:
    root = Path(directory).resolve()
    results = []
    for command in commands:
        try:
            # shell=True is intentional: commands are user-supplied local checks,
            # and this preserves pipes/PowerShell-compatible commands on Windows.
            p = subprocess.run(command, cwd=root, shell=True, text=True, encoding="utf-8", errors="replace",
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
            results.append({"command": command, "returncode": p.returncode, "output": p.stdout})
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout or ""
            if isinstance(partial, bytes): partial = partial.decode("utf-8", "replace")
            results.append({"command": command, "returncode": 124, "output": partial + "\nTIMEOUT"})
        except OSError as exc:
            results.append({"command": command, "returncode": 127, "output": str(exc)})
    return {"directory": str(root), "commit": _commit(root), "ok": all(x["returncode"] == 0 for x in results), "results": results}


def run_pair(spec: PairSpec, output: str | Path) -> dict[str, Any]:
    checks = validate_spec(spec, require_decision=False)
    if not all(x["ok"] for x in checks):
        return {"ok": False, "checks": checks, "error": "输入校验未通过"}
    out = Path(output).resolve(); out.mkdir(parents=True, exist_ok=True)
    a = run_checks(spec.a_directory, spec.commands, spec.timeout_seconds)
    b = run_checks(spec.b_directory, spec.commands, spec.timeout_seconds)
    # Always record the revision that was actually executed.
    spec.a_commit = a["commit"]; spec.b_commit = b["commit"]
    spec.completed_at = datetime.now(timezone.utc).isoformat()
    result = {"ok": bool(a["ok"] and b["ok"]), "checks": checks, "A": a, "B": b, "spec": asdict(spec)}
    (out / "pair_run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for side, data in (("A", a), ("B", b)):
        (out / f"{side.lower()}-checks.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        with (out / f"{side.lower()}-checks.log").open("w", encoding="utf-8") as f:
            for item in data["results"]:
                f.write(f"$ {item['command']}\n{item['output']}\n[exit={item['returncode']}]\n\n")
    return result


def export_dataset(spec: PairSpec, run_result: dict[str, Any], output: str | Path) -> Path:
    out = Path(output); out.parent.mkdir(parents=True, exist_ok=True)
    row = {k: "" for k in DATASET_COLUMNS}
    row.update({"prompt": spec.prompt, "evaluator": spec.evaluator, "generation_mode": spec.generation_mode,
                "provider": spec.provider, "difficulty": spec.difficulty, "environment": spec.environment,
                "os": spec.os_name, "dependencies": spec.dependencies, "baseline_commit": spec.baseline_commit,
                "A_directory": spec.a_directory, "A_commit": spec.a_commit, "A_session_id": spec.a_session_id,
                "A_trace_url": spec.a_trace_url, "A_video_url": spec.a_video_url, "B_directory": spec.b_directory,
                "B_commit": spec.b_commit, "B_session_id": spec.b_session_id, "B_trace_url": spec.b_trace_url,
                "B_video_url": spec.b_video_url, "winner": spec.winner, "label": spec.label,
                "rationale": spec.rationale, "A_check_status": "PASS" if run_result.get("A", {}).get("ok") else "FAIL",
                "B_check_status": "PASS" if run_result.get("B", {}).get("ok") else "FAIL",
                "created_at": spec.created_at, "completed_at": spec.completed_at})
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=DATASET_COLUMNS); w.writeheader(); w.writerow(row)
    return out

"""Real CLI metadata preflight in disposable homes, without model requests."""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from agent_trace_kit.run_policy import preflight, private_write


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="atk-codex-probe-") as directory:
        root = Path(directory)
        home, work = root / "home", root / "work"
        home.mkdir()
        work.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(
            ("CODEX_", "ATK_CODEX_", "OPENAI_", "ANTHROPIC_", "CLAUDE_"))}
        env["CODEX_HOME"] = str(home)
        first = preflight(args.codex, "codex", env, [], work)
        second = preflight(args.codex, "codex", env, [], work)
        assert first["bundled_skills_sha256"] == second["bundled_skills_sha256"]
        skill = next((home / "skills" / ".system").rglob("SKILL.md"))
        original = skill.read_bytes()
        skill.write_text("MUST launch another reviewer", encoding="utf-8")
        modified_rejected = False
        try:
            preflight(args.codex, "codex", env, [], work)
        except ValueError as exc:
            modified_rejected = "不一致" in str(exc)
        assert modified_rejected
        assert skill.read_bytes() != original  # Rejection preserves evidence.
        report = {"passed": True, "capability_check": first["capability_check"],
                  "skills_check": first["skills_check"], "skills_disabled": first["skills_disabled"],
                  "bundled_skills_sha256": first["bundled_skills_sha256"],
                  "repeat_preflight": "passed", "modified_bundle_rejected": modified_rejected,
                  "model_requests": 0, "model_completion_test": "NOT_RUN"}
        private_write(args.output, json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Strict, explainable TOML configuration for the Windows collector."""
from __future__ import annotations
import copy, os, tomllib
from pathlib import Path
try:
    import tomli_w
except ImportError:
    tomli_w = None

DEFAULTS = {
    "data_root": str(Path.home() / ".agent-trace-kit"),
    "workspace_root": str(Path.cwd()),
    "poll_interval_seconds": 2.0,
    "debounce_seconds": 2.0,
    "stable_window_seconds": 3.0,
    "provider": "codex",
    "mode": "observe",
    "backend": "native",
    "archive_only_bound": True,
    "max_file_size_mb": 256,
    "display_timezone": "Asia/Shanghai",
    "allow_push": False,
    "csv_encoding": "utf-8-sig",
}
ALLOWED = set(DEFAULTS) | {"sqlite_path", "log_dir", "session_roots", "allowed_projects", "excluded_projects", "python_path", "git_path", "powershell_path", "cli_path", "collect_subagents", "monitor_source_changes", "bad_line_policy", "unknown_event_policy", "expected_version", "extra_argv", "approved_profile_ref", "inherited_env", "secret_env_refs", "gateway_ref", "model", "context_window", "permission_policy", "startup_timeout_seconds", "wall_clock_timeout_seconds", "concurrency_limit", "git_mode", "initial_sha", "remote", "branch_pattern", "collector_commit_message", "sensitive_excludes", "validation_commands", "validation_runs", "validation_cwd", "validation_timeout_seconds", "recording", "upload", "export_columns", "required_fields", "custom_fields", "draft", "annotator", "pairwise_policy"}
ENUMS = {"provider": {"claude", "codex"}, "mode": {"observe", "managed", "import"}, "backend": {"native", "wsl"}, "bad_line_policy": {"preserve", "fail", "skip"}, "unknown_event_policy": {"preserve", "fail"}, "git_mode": {"readonly", "managed_snapshot"}, "csv_encoding": {"utf-8", "utf-8-sig"}}

def _flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict): out.update(_flatten(v, key))
        else: out[key] = v
    return out

def load_config(paths: list[Path] | None = None, cli: dict | None = None):
    result = copy.deepcopy(DEFAULTS); sources = {k: "defaults" for k in result}
    for path in paths or []:
        if not path or not Path(path).exists(): continue
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        unknown = set(_flatten(data)) - ALLOWED
        if unknown: raise ValueError(f"unknown configuration key(s): {', '.join(sorted(unknown))}")
        for k, v in _flatten(data).items(): result[k] = v; sources[k] = str(path)
    for k, v in (cli or {}).items():
        if v is not None: result[k] = v; sources[k] = "CLI"
    for k, choices in ENUMS.items():
        if k in result and result[k] not in choices: raise ValueError(f"invalid {k}: {result[k]}")
    if not isinstance(result.get("poll_interval_seconds"), (int, float)): raise ValueError("poll_interval_seconds must be numeric")
    for k in ("data_root", "workspace_root"): result[k] = os.path.expandvars(os.path.expanduser(str(result[k])))
    return result, sources

def redacted(config):
    out = copy.deepcopy(config)
    for k in list(out):
        if any(x in k.lower() for x in ("secret", "credential", "token", "password", "key")): out[k] = "<redacted>"
    return out

def write_toml(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for k, v in data.items():
        if isinstance(v, bool): val = "true" if v else "false"
        elif isinstance(v, (int, float)): val = str(v)
        elif isinstance(v, list): val = "[" + ", ".join('"' + str(x).replace('"','\\"') + '"' for x in v) + "]"
        else: val = '"' + str(v).replace('\\', '\\\\').replace('"','\\"') + '"'
        lines.append(f"{k} = {val}")
    path.write_text("# AgentTraceKit Windows configuration\n" + "\n".join(lines) + "\n", encoding="utf-8")

def validate_config(paths): return load_config(paths)[0]

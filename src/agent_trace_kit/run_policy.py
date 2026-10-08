"""Child-only CLI configuration, capability preflight and policy receipts."""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .compliance import CODEX_OVERRIDES, POLICY_VERSION, PROTOCOL

_CLAUDE_AUTH = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL", "CLAUDE_CODE_OAUTH_TOKEN",
}


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_home(home: Path) -> None:
    # Fail closed; never delete or silently overwrite a contaminated directory.
    forbidden = {"agents", "rules", "hooks", "plugins", "AGENTS.md",
                 "CLAUDE.md", "instructions.md", "settings.json", "settings.local.json", ".mcp.json"}
    bad = sorted(p.name for p in home.iterdir() if p.name.casefold() in {s.casefold() for s in forbidden})
    if bad:
        raise ValueError("正式 CLI 配置目录含个人指令或扩展，停止运行：" + ", ".join(bad))
    skills = home / "skills"
    if skills.exists() or skills.is_symlink():
        if skills.is_symlink() or not skills.is_dir() or any(
            p.name != ".system" or not p.is_dir() or p.is_symlink() for p in skills.iterdir()
        ):
            raise ValueError("正式 CLI 配置目录含个人指令或扩展，停止运行：skills")
        # Structural allowance only. Preflight verifies bundled bytes against
        # this CLI's fresh installation, then disables all registered skills.
        _tree_hashes(skills)


def _tree_hashes(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    hashes = {}
    for path in root.rglob("*"):
        stat = path.lstat()
        if path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise ValueError("正式 CLI skills 含链接或重解析点，拒绝运行")
        if path.is_file():
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _skills_list(command: str, env: dict, flags: list[str], cwd: Path) -> list[dict]:
    """Local app-server metadata only; never create a thread or model turn."""
    from .procmon import hidden_console_kwargs, kill_tree
    process = subprocess.Popen(
        [command, *flags, "app-server", "--strict-config"], cwd=cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="strict", **hidden_console_kwargs(),
    )
    messages = queue.Queue()

    def read():
        try:
            for line in process.stdout:
                messages.put(line)
        except (OSError, UnicodeError):
            pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        requests = [
            {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "atk_preflight", "version": "1"}}},
            {"method": "initialized"},
            {"id": 2, "method": "skills/list", "params": {"cwds": [str(cwd)], "forceReload": True}},
        ]
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if line is None:
                break
            response = json.loads(line)
            if response.get("id") == 2:
                data = response.get("result", {}).get("data")
                if not isinstance(data, list) or len(data) != 1 or data[0].get("errors"):
                    break
                skills = data[0].get("skills")
                if isinstance(skills, list) and all(isinstance(s, dict) and isinstance(s.get("path"), str)
                                                  and isinstance(s.get("enabled"), bool) for s in skills):
                    return skills
                break
    except (queue.Empty, ValueError, OSError):
        pass
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            kill_tree(process.pid)
            process.wait(timeout=5)
        reader.join(timeout=1)
        process.stdout.close()
    raise ValueError("CLI 合规预检无法核验有效 skills 清单，未启动模型")


def _codex_skills(command: str, env: dict, flags: list[str], cwd: Path) -> dict:
    home = Path(env["CODEX_HOME"])
    # A disposable CODEX_HOME lets the same installed binary supply the trusted
    # bundled files. No personal credentials/configuration are copied here.
    with tempfile.TemporaryDirectory(prefix="atk-system-skills-") as directory:
        clean_home = Path(directory)
        clean_env = {**env, "CODEX_HOME": directory}
        _skills_list(command, clean_env, CODEX_OVERRIDES, clean_home)
        trusted = _tree_hashes(clean_home / "skills" / ".system")
    if not trusted:
        raise ValueError("CLI 未提供可核验的内置 skills，拒绝正式运行")
    initial = _skills_list(command, env, [*flags, *CODEX_OVERRIDES], cwd)
    check_home(home)
    if _tree_hashes(home / "skills" / ".system") != trusted:
        raise ValueError("CLI 内置 skills 与当前安装包不一致，拒绝正式运行；保留原文件")
    paths = sorted({item["path"] for item in initial})
    override = "skills.config=[" + ",".join(
        "{path=" + json.dumps(path) + ",enabled=false}" for path in paths
    ) + "]"
    extra = ["-c", override]
    effective = _skills_list(command, env, [*flags, *CODEX_OVERRIDES, *extra], cwd)
    if {s["path"] for s in effective} != set(paths) or any(s["enabled"] for s in effective):
        raise ValueError("CLI 合规预检未证明所有 skills 已禁用，拒绝正式运行")
    return {"extra_args": extra, "skills_check": "passed", "skills_disabled": len(paths),
            "bundled_skills_sha256": hashlib.sha256(json.dumps(trusted, sort_keys=True).encode()).hexdigest()}


def _json(path: Path) -> dict:
    if not path.is_file():
        return {}
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("继承认证配置必须是对象；请使用交付台专用连接")
    return value


def isolated_inherit(agent: str, home: Path, original: dict[str, str]) -> tuple[dict, list[str]]:
    """Copy authentication/provider fields only, never rules, MCP or hooks."""
    env = {k: v for k, v in original.items() if not k.upper().startswith(
        ("ANTHROPIC_", "OPENAI_", "AZURE_OPENAI_", "CODEX_", "CLAUDE_", "ATK_CODEX_"))}
    if agent == "claude":
        source = Path(original.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        settings = _json(source / "settings.json")
        saved_env = settings.get("env", {})
        if isinstance(saved_env, dict):
            env.update({k: str(v) for k, v in saved_env.items() if k in _CLAUDE_AUTH})
        env.update({k: v for k, v in original.items() if k in _CLAUDE_AUTH})
        auth = _json(source / ".credentials.json")
        if isinstance(auth.get("claudeAiOauth"), dict):
            private_write(home / ".credentials.json", json.dumps({"claudeAiOauth": auth["claudeAiOauth"]}))
        env["CLAUDE_CONFIG_DIR"] = str(home)
        return env, ["--setting-sources", ""]
    source = Path(original.get("CODEX_HOME") or Path.home() / ".codex")
    config = {}
    if (source / "config.toml").is_file():
        try:
            import tomllib
        except ImportError:
            raise ValueError("隔离继承 Codex 配置需要 Python 3.11+；请配置交付台专用连接") from None
        config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8-sig"))
    text = 'cli_auth_credentials_store = "file"\n'
    for key in ("model", "model_provider", "model_reasoning_effort", "model_reasoning_summary"):
        value = config.get(key)
        if isinstance(value, str):
            text += f"{key} = {json.dumps(value)}\n"
    provider = config.get("model_provider", "openai")
    if not isinstance(provider, str):
        raise ValueError("Codex 供应商配置无效；请配置交付台专用连接")
    providers = config.get("model_providers", {})
    selected = providers.get(provider, {}) if isinstance(providers, dict) else {}
    if isinstance(selected, dict) and selected:
        text += f"[model_providers.{json.dumps(provider)}]\n"
        for key in ("name", "base_url", "env_key", "wire_api", "requires_openai_auth",
                    "experimental_bearer_token"):
            value = selected.get(key)
            if isinstance(value, (str, bool)):
                text += f"{key} = {json.dumps(value)}\n"
        # Unsupported authentication must not silently select a different model
        # endpoint. A desk-owned connection is the explicit migration path.
        if any(selected.get(k) for k in ("http_headers", "env_http_headers", "query_params")):
            raise ValueError("此供应商使用自定义认证字段，无法安全继承；请配置交付台专用连接")
        key = selected.get("env_key")
        if isinstance(key, str) and key in original:
            env[key] = original[key]
    if os.name == "nt":
        text += '[windows]\nsandbox = "elevated"\n'
    private_write(home / "config.toml", text)
    auth = _json(source / "auth.json")
    retained = {k: v for k, v in auth.items() if k in {"auth_mode", "OPENAI_API_KEY", "tokens", "last_refresh"}}
    if retained:
        private_write(home / "auth.json", json.dumps(retained))
    if original.get("OPENAI_API_KEY"):
        env["OPENAI_API_KEY"] = original["OPENAI_API_KEY"]
    env["CODEX_HOME"] = str(home)
    return env, []


def frozen_inherit(agent: str, home: Path, original: dict[str, str]) -> tuple[dict, list[str]]:
    """Called under the connection lock; A/B use the same sanitized provider."""
    snapshot = home / "inherited-auth-env.json"
    config = home / "config.toml"
    from .cli_config import _protect, _unprotect
    if snapshot.is_file():
        saved = _json(snapshot)
        if saved.get("agent") != agent or saved.get("config_sha256") != (
            hashlib.sha256(config.read_bytes()).hexdigest() if config.is_file() else ""
        ):
            raise ValueError("冻结的继承配置已变化，拒绝运行；请新建任务或改用专用连接")
        clean = {k: v for k, v in original.items() if not k.upper().startswith(
            ("ANTHROPIC_", "OPENAI_", "AZURE_OPENAI_", "CODEX_", "CLAUDE_", "ATK_CODEX_"))}
        clean.update(json.loads(_unprotect(saved["protected_env"])))
        return clean, saved["flags"]
    env, flags = isolated_inherit(agent, home, original)
    keys = _CLAUDE_AUTH | {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENAI_API_KEY"}
    if agent == "codex" and config.is_file():
        match = re.search(r'^env_key = (".*")$', config.read_text(encoding="utf-8"), re.M)
        if match:
            keys.add(json.loads(match[1]))
    private_write(snapshot, json.dumps({"agent": agent, "protected_env": _protect(json.dumps({k: v for k, v in env.items() if k in keys})),
                                       "flags": flags, "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest() if config.is_file() else ""}))
    return env, flags


def preflight(command: str, agent: str, env: dict, connection_args: list[str], cwd: str | Path | None = None) -> dict:
    """Read CLI capabilities without a model request; unknown output is failure."""
    from .procmon import run_hidden
    args = ([command, *connection_args, *CODEX_OVERRIDES, "features", "list"] if agent == "codex"
            else [command, "--help"])
    result = run_hidden(args, env=env, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=20)
    output = result.stdout
    if result.returncode:
        raise ValueError("CLI 合规预检失败；未启动模型，请检查 CLI 版本和配置")
    if agent == "codex":
        for feature in ("multi_agent", "multi_agent_v2"):
            if not re.search(rf"^{feature}\s+\S+\s+false\s*$", output, re.M):
                raise ValueError(f"CLI 合规预检未证明 {feature}=false，拒绝正式运行")
    elif not all(flag in output for flag in ("--tools", "--disallowedTools", "--mcp-config", "--strict-mcp-config", "--setting-sources", "--append-system-prompt")):
        raise ValueError("Claude CLI 不支持所需禁委派参数，拒绝正式运行")
    capability = {"agent": agent, "capability_check": "passed", "capability_sha256": hashlib.sha256(output.encode()).hexdigest()}
    if agent == "codex":
        if not env.get("CODEX_HOME") or cwd is None:
            raise ValueError("CLI 合规预检缺隔离目录或工作区，拒绝正式运行")
        capability.update(_codex_skills(command, env, connection_args, Path(cwd)))
    return capability


def write_receipt(path: Path, agent: str, env: dict, capability: dict, args: list[str]) -> None:
    home = Path(env["CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR"])
    config = home / "config.toml"
    # Do not serialize keys, auth files, relay credentials or arbitrary argv.
    receipt = {**{k: v for k, v in capability.items() if k != "extra_args"}, "policy_version": POLICY_VERSION, "cli_home": str(home),
               "protocol_sha256": hashlib.sha256(PROTOCOL.encode()).hexdigest(),
               "config_sha256": hashlib.sha256(config.read_bytes()).hexdigest() if config.is_file() else "",
               "delegation_disabled": True,
               "launch_args_sha256": hashlib.sha256(json.dumps(args).encode()).hexdigest()}
    private_write(path, json.dumps(receipt, ensure_ascii=False, indent=2))

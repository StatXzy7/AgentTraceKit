"""Desk-owned CLI connections, write-only credentials and model discovery."""

from __future__ import annotations

import base64
import ctypes
import ipaddress
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

_LOCK = threading.RLock()
ENGINES = ("codex", "claude")
KEY_ENV = "ATK_CODEX_API_KEY"


def validate_model(value: str) -> str:
    value = str(value or "").strip()
    if value and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\[\]-]{0,199}", value):
        raise ValueError(
            "模型 ID 只能包含字母、数字和 . _ : / @ + - [ ]，最长 200 字符"
        )
    return value


def validate_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("接口地址无效") from None
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 33 for c in value)
        or "\\" in value
        or (port is not None and port < 1)
    ):
        raise ValueError("请填写不含凭据、查询参数或片段的 API Base URL")
    loopback = parsed.hostname == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("远端接口必须使用 HTTPS；本机测试允许 HTTP")
    if parsed.path.endswith(
        ("/responses", "/chat/completions", "/messages", "/models")
    ):
        raise ValueError(
            "请填写 API 基址，不要包含 /responses、/messages 或 /models 等端点"
        )
    return value


def _key(value: str) -> str:
    value = str(value or "").strip()
    if len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise ValueError("API Key 格式无效（不能包含空格或换行）")
    return value


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    """Windows user-bound protection; never invoke a shell with a credential."""
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, output = Blob(len(data), buffer), Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [
        ctypes.POINTER(Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(Blob),
    ]
    function.restype = wintypes.BOOL
    if not function(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)
    ):
        raise RuntimeError(
            "无法读取或保护 CLI Key，请在运行交付台的同一 Windows 账号重新配置"
        )
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.WinDLL("kernel32", use_last_error=True).LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


def _protect(key: str) -> dict:
    if os.name == "nt":
        return {
            "encoding": "dpapi",
            "value": base64.b64encode(_dpapi(key.encode())).decode("ascii"),
        }
    return {"encoding": "file", "value": key}


def _unprotect(secret: dict) -> str:
    if secret.get("encoding") == "dpapi":
        if os.name != "nt":
            raise ValueError("Windows 加密 Key 无法在此系统读取，请重新配置")
        return _dpapi(base64.b64decode(secret["value"]), decrypt=True).decode("utf-8")
    if secret.get("encoding") == "file":
        return secret["value"]
    raise ValueError("Key 存储格式无效，请重新配置")


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".cli-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CliConnections:
    """Append-only connection revisions: a Pair keeps its chosen endpoint/key."""

    def __init__(self, home: Path):
        self.home = Path(home)
        self.path = self.home / "cli-connections.json"

    def _read(self) -> dict:
        if not self.path.exists():
            return {"active": {}, "revisions": {}}
        return json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def _engine(agent: str) -> str:
        if agent not in ENGINES:
            raise ValueError("CLI 必须是 codex 或 claude")
        return agent

    def _current(self, agent: str) -> dict:
        self._engine(agent)
        data = self._read()
        return data["revisions"].get(
            data["active"].get(agent),
            {
                "id": "",
                "agent": agent,
                "mode": "inherit",
                "base_url": "",
                "model": "",
                "auth": "bearer" if agent == "codex" else "api_key",
            },
        )

    @staticmethod
    def _public(profile: dict) -> dict:
        return {
            **{k: v for k, v in profile.items() if k != "secret"},
            "key_configured": bool(profile.get("secret")),
        }

    def public(self) -> dict:
        with _LOCK:
            return {agent: self._public(self._current(agent)) for agent in ENGINES}

    def _draft(self, agent: str, body: dict) -> tuple[dict, str]:
        current = self._current(agent)
        mode = body.get("mode", current["mode"])
        if mode not in ("inherit", "project"):
            raise ValueError("连接模式必须是继承本机或交付台专用")
        base = str(body.get("base_url", current.get("base_url", ""))).strip()
        auth = body.get("auth", current.get("auth"))
        if auth not in (("bearer",) if agent == "codex" else ("api_key", "bearer")):
            raise ValueError("Key 认证类型无效")
        model = validate_model(body.get("model", current.get("model", "")))
        key = _key(body.get("api_key", ""))
        if mode == "project":
            base = validate_url(base)
            if agent == "claude":
                # Claude's SDK appends /v1/messages itself. Accept pasted API
                # prefixes while keeping runtime and catalog paths consistent.
                base = base.removesuffix("/v1")
            if not key and current.get("secret"):
                if base != current.get("base_url") or auth != current.get("auth"):
                    raise ValueError("更换接口地址或认证类型时请重新填写 Key")
                key = _unprotect(current["secret"])
            if not key:
                raise ValueError("请填写此 CLI 专用的 API Key")
        else:
            base, key = "", ""
        return {
            "agent": agent,
            "mode": mode,
            "base_url": base,
            "auth": auth,
            "model": model,
        }, key

    def save(self, agent: str, body: dict) -> dict:
        with _LOCK:
            profile, key = self._draft(agent, body)
            profile["id"] = uuid.uuid4().hex
            if key:
                profile["secret"] = _protect(key)
            data = self._read()
            data["revisions"][profile["id"]] = profile
            data["active"][agent] = profile["id"]
            _atomic_json(self.path, data)
            return self._public(profile)

    def bind(self, agent: str, model: str = "") -> dict:
        with _LOCK:
            profile = self._public(self._current(agent))
        profile["model"] = validate_model(model or profile.get("model", ""))
        if profile["mode"] == "project" and not profile["model"]:
            raise ValueError("交付台专用连接需要选择本任务使用的模型")
        return profile

    def runtime(
        self, job: dict, env: dict[str, str]
    ) -> tuple[dict[str, str], list[str]]:
        """Resolve the frozen revision and return child-only env and CLI flags."""
        prefixes = job.get("runtime_path_prepend") or []
        if prefixes:
            if not isinstance(prefixes, list) or any(
                not isinstance(p, str) or os.pathsep in p or not Path(p).is_dir()
                for p in prefixes
            ):
                raise ValueError("任务运行环境的 PATH 前置目录无效")
            env = dict(env)
            path_key = next((k for k in env if k.upper() == "PATH"), "PATH")
            env[path_key] = os.pathsep.join([*prefixes, env.get(path_key, "")])
        binding = job.get("cli_connection") or {}
        if binding.get("mode") != "project":
            if job.get("id") and job.get("agent"):
                from .run_policy import check_home, frozen_inherit
                home = (self.home / "cli-runtime" / job["agent"] / job["id"]).resolve()
                home.mkdir(parents=True, exist_ok=True)
                check_home(home)
                with _LOCK:
                    return frozen_inherit(job["agent"], home, env)
            return env, []
        with _LOCK:
            profile = self._read()["revisions"].get(binding.get("id"))
            if not profile or profile["agent"] != job["agent"]:
                raise ValueError("任务绑定的 CLI 连接不存在；请重新创建任务并选择连接")
            key = _unprotect(profile["secret"])
        agent = profile["agent"]
        home = (self.home / "cli-runtime" / agent / job["id"]).resolve()
        home.mkdir(parents=True, exist_ok=True)
        from .run_policy import check_home
        check_home(home)
        env = dict(env)
        for name in list(env):
            upper = name.upper()
            if upper.startswith(
                (
                    "ANTHROPIC_",
                    "OPENAI_",
                    "AZURE_OPENAI_",
                    "CODEX_",
                    "CLAUDE_CODE_OAUTH",
                    "CLAUDE_CODE_USE_",
                    "CLAUDE_",
                )
            ) or upper in ("CLAUDE_CONFIG_DIR", KEY_ENV):
                env.pop(name)
        if agent == "codex":
            env.update(CODEX_HOME=str(home), ATK_CODEX_API_KEY=key)
            config = (
                'model_provider = "pair_desk"\ncli_auth_credentials_store = "ephemeral"\n'
                '[model_providers.pair_desk]\nname = "Pair Desk"\n'
                f'base_url = {json.dumps(profile["base_url"])}\n'
                f'env_key = "{KEY_ENV}"\nwire_api = "responses"\nrequires_openai_auth = false\n'
                # PairRunner owns the bounded backoff; do not multiply retries.
                "request_max_retries = 0\nstream_max_retries = 0\n"
                "[shell_environment_policy]\nignore_default_excludes = false\n"
            )
            if os.name == "nt":
                # Isolated CODEX_HOME does not inherit the user's Windows
                # sandbox backend. Without it, unattended shell calls fail
                # closed even when --sandbox workspace-write is selected.
                config += '[windows]\nsandbox = "elevated"\n'
            # Identical for both sides; atomic replace avoids partial config reads.
            fd, temporary = tempfile.mkstemp(dir=home, suffix=".toml")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(config)
                os.replace(temporary, home / "config.toml")
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return env, ["-c", 'model_provider="pair_desk"']
        env.update(
            CLAUDE_CONFIG_DIR=str(home),
            ANTHROPIC_BASE_URL=profile["base_url"].removesuffix("/v1"),
        )
        env[
            (
                "ANTHROPIC_AUTH_TOKEN"
                if profile["auth"] == "bearer"
                else "ANTHROPIC_API_KEY"
            )
        ] = key
        return env, ["--setting-sources", ""]

    def models(self, agent: str, body: dict) -> dict:
        """Read the configured provider's catalog, without saving or generating."""
        with _LOCK:
            profile, key = self._draft(agent, body)
        if profile["mode"] != "project":
            raise ValueError("请先选择交付台专用连接，并填写接口地址和 Key")
        base = profile["base_url"]
        endpoint = base + (
            "/v1/models"
            if agent == "claude" and not base.endswith("/v1")
            else "/models"
        )
        headers = {"Accept": "application/json"}
        if agent == "claude":
            headers["anthropic-version"] = "2023-06-01"
        headers["x-api-key" if profile["auth"] == "api_key" else "Authorization"] = (
            key if profile["auth"] == "api_key" else "Bearer " + key
        )
        models: set[str] = set()
        after = ""
        opener = build_opener(_NoRedirect())
        for _ in range(10):
            url = endpoint + ("?" + urlencode({"after_id": after}) if after else "")
            try:
                with opener.open(Request(url, headers=headers), timeout=10) as response:
                    raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("模型列表响应过大")
                data = json.loads(raw)
            except HTTPError as exc:
                code = exc.code
                exc.close()
                hints = {
                    401: "Key 无效或已过期",
                    403: "Key 无权访问模型列表",
                    404: "服务未提供模型列表接口，可手动填写模型 ID",
                }
                raise ValueError(
                    f"模型查询 HTTP {code}：{hints.get(code, '查询失败；不跟随重定向，请检查接口地址')}"
                ) from None
            except (
                URLError,
                TimeoutError,
                OSError,
                UnicodeError,
                json.JSONDecodeError,
            ):
                raise ValueError(
                    "模型查询失败：连接超时、网络错误或响应不是有效 JSON；可手动填写模型 ID"
                ) from None
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                raise ValueError("服务返回的模型列表格式不支持，可手动填写模型 ID")
            for item in data["data"]:
                model = item.get("id") if isinstance(item, dict) else None
                if not isinstance(model, str) or key in model:
                    continue
                try:
                    models.add(validate_model(model))
                except ValueError:
                    continue
            models.discard("")
            if not data.get("has_more"):
                return {"models": sorted(models), "truncated": False}
            last = data.get("last_id")
            if (
                not isinstance(last, str)
                or not last
                or last == after
                or len(last) > 200
            ):
                raise ValueError("模型列表分页信息无效，可手动填写模型 ID")
            after = last
        return {"models": sorted(models), "truncated": True}

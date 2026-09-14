# AgentTraceKit

> 一条命令，把 Coding Agent 会话变成可验证、可阅读、可标注的轨迹包。

无需数据库、代理、后端，也不需要了解 JSONL。

```powershell
pipx install agent-trace-kit
atk
```

运行 `atk doctor` 检查环境，`atk collect --latest` 自动采集最新 Codex 会话，`atk collect --input FILE` 可指定文件，`atk verify BUNDLE` 独立校验，`atk open` 打开报告。默认只读本机文件，不上传、不 telemetry。分享前请检查 raw 内容。

v0.1.0 正式支持 Codex CLI；Claude Code、ZCode 和其他 Agent 计划支持。

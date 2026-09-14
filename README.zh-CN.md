# AgentTraceKit

> 一条命令，把 Coding Agent 会话变成可验证、可阅读、可标注的轨迹包。

无需数据库、代理、后端，也不需要了解 JSONL。

```powershell
pipx install agent-trace-kit
atk
```

运行 `atk doctor` 检查环境，`atk collect --latest` 自动采集最新 Codex 会话，`atk collect --input FILE` 可指定文件，`atk verify BUNDLE` 独立校验，`atk open` 打开报告。默认只读本机文件，不上传、不 telemetry。分享前请检查 raw 内容。

v0.3.0 正式支持 Codex CLI；Claude Code、ZCode 和其他 Agent 计划支持。

使用 `atk browse` 打开可视化 session 选择器，点击 **Collect & Review** 即可采集并进入完整浏览器工作台。工作台包含 Overview、Timeline、Evidence、Annotation、Files 和 Verify 按钮，日常操作无需继续使用命令行。使用 `atk view BUNDLE` 可直接打开工作台，`atk annotate BUNDLE` 可单独进入标注界面。标注保存在 `annotation/annotations.jsonl`，并通过稳定的 event ID 和原始行号回溯证据。多个 bundle 可用 `atk corpus index DIRECTORY` 建立本地可重建索引。

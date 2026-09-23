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
## Pair 交付台（本地 A/B 跑题）

仓库根目录**没有**名为「Pair 交付台」的 exe 或文件夹，这是正常的。交付台是用本仓库源码跑起来的本地网页，不是单独安装包。

```powershell
cd D:\myprojects\AgentTraceKit
python -m pip install -e .
python -m agent_trace_kit.desk          # 任务栏窗口 + 打开 http://127.0.0.1:8765
python -m agent_trace_kit.desk status
python -m agent_trace_kit.desk stop
```

也可以双击 `scripts\windows\start-desk.vbs`。关浏览器、关这个仓库文件夹都不会停交付台；关掉任务栏上的「Pair 交付台」或执行 `stop` 才会停。

| 你想找的 | 实际位置 |
| --- | --- |
| 源码 | `src/agent_trace_kit/desk.py`、`desk_app.py`、`desk_service.py`、`desk_store.py` |
| 启动脚本 | `scripts/windows/start-desk.vbs` |
| 任务、A/B 工作区、轨迹、锁文件 | `C:\AgentTraceKit-data\desk\`（在 git 仓库外面；可用环境变量 `ATK_DESK_HOME` 改） |
| 完整操作说明 | [docs/PAIR_DESK.md](docs/PAIR_DESK.md) |

## AB 目录评测与上传

如果同一提示词需要在两个本地实现目录运行，可以直接启动本地 UI：

```powershell
python -m pip install -e .
atk pair-ui
```

在页面填入 A/B 目录、环境、统一检查命令和结论。`检查缺失项` 只做输入校验；`运行 A/B 检查` 会按同一顺序执行命令并保存 `a-checks.log`、`b-checks.log`、`pair_run.json`；`生成数据 CSV` 会输出包含提示词、环境、提交、轨迹/视频 URL、结论、理由和两侧检查状态的行。填写一个已有 Git clone 后点击 `生成并上传`，工具会复制证据、提交并执行 `git push`。认证使用本机 Git credential helper 或 SSH agent，不读取或保存 token。

上传前应先人工确认原始轨迹和视频 URL，不要把含敏感信息的文件放入目标仓库。

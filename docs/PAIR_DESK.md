# Pair 交付台使用说明

交付台把同题 A/B 的冻结基线、执行、取证、产物提交、演示、评审和 26 列导出串起来。完整模块关系见 [版本逻辑](ARCHITECTURE.md)，条件和限制见 [可用性与改进](USABILITY_AND_IMPROVEMENTS.md)。

## 启动与数据

```sh
python -m pip install -e .
python -m agent_trace_kit.desk
python -m agent_trace_kit.desk status
python -m agent_trace_kit.desk stop
```

访问 <http://127.0.0.1:8765/>。Windows 可双击 `scripts/windows/start-desk.vbs`；Linux 部署见 [部署指南](LINUX_DEPLOYMENT.md)。关闭浏览器不会停止后台执行；前台调试用 `python -m agent_trace_kit.desk foreground --no-browser`。

Windows 默认数据目录 `C:/AgentTraceKit-data/desk/`，Linux 默认 `~/.local/share/agenttracekit/desk/`。启动前设置 `ATK_DESK_HOME` 可改变位置。任务和设置原子保存；`jobs/`、`workspaces/`、`evidence/`、`cli-runtime/` 位于数据目录，凭据也留在该目录的私有文件内。

## 连接与模型

设置页分别配置 Codex / Claude。支持继承本机允许的认证/provider/model 字段，或使用交付台专用 Base URL、Key 和模型。个人 rules、agents、hooks、插件、MCP 不继承；隔离不能安全完成时停止并报告。

- Codex 接口须兼容 Responses API；Claude 须兼容 Anthropic Messages API。模型列表查询只查服务目录，不代表所有返回模型都支持 CLI。
- 每条 Pair 固定连接版本与模型，A/B 使用同一 CLI。批量 TSV 第 6 列可填 `codex` / `claude`，第 9 列可选模型。后台更换连接或默认模型只影响新 Pair；吊销的历史 Key 不会自动换身份重试。
- Key 只写不回显；Windows 用当前用户 DPAPI，Linux 用权限 0600 文件。专用 Codex 的 CLI 仅获取本地 relay 的随机令牌，上游 Key 留在 relay 进程。
- Codex 默认 `workspace-write`；Windows 的专用配置显式启用原生 sandbox 后端。Claude 默认权限模式为 `bypassPermissions`，可以在设置中调整。不能把独立目录视为 OS 沙箱。

## 选择基线来源

支持自动新建 GitHub 仓库、已有 GitHub 仓库的指定 commit，以及本地独立 Git clone。自动建仓库和推送需要当前账号的 `gh` / Git 权限；请明确选择公开或私有。已有 commit 可粘贴 permalink 或输入仓库与 SHA；批量 TSV 第 7 列指定 commit、第 8 列可填分支后缀。

准备完成后冻结完整 `baseline_sha`。A/B 与干净重跑均从该 SHA 创建，产物分支为 `<pair-id>[-suffix]-a/-b`。本地基线后续新增提交不会改变已有 Pair；linked worktree 或共享 Git 元数据须先克隆成独立仓库；本地/新建复制模式暂不支持 tracked submodule，请使用 GitHub 指定 commit 模式。副本清理仅发生在新创建的独立侧目录。

## 执行、失败和重跑

默认最多 2 个 Pair / 4 个 CLI 侧；设置上限为 16 Pair / 32 侧。上限是本机名额，实际应按内存、CPU 和供应商限额调整。默认单次超时 1800 秒，单侧总预算 14400 秒，等待退避计入预算。具体重试次数和恢复行为见 [运行策略](COMPLETION_PROFILE.md)。

每次尝试保留事件流、原始会话快照、退出码、会话 ID 与策略收据。成功须同时通过退出状态、完成标记、原始会话身份、工作目录和提示词核验。中止和失败保留记录，不绑定另一个旧会话冒充完成。

默认干净单轮协议；手动重跑保留旧工作区、失败记录和旧评审后从冻结基线重新开始，新产物需重新评审。显式续接模式使用不同策略，额外轮次保留在检查表，不能作为单轮轨迹。服务恢复不自动增加预算。

## 演示与录屏

Windows 内置 FFmpeg 桌面/窗口录屏；Linux 可在独立副本中自动演示，配置见 [Linux 演示](LINUX_DEMO.md)。视频硬上限 89 秒。静态页能加载和滚动只证明这两步，终端输出没展示完整则报告不完整。

失败侧可以保留事后冻结快照供复核；来源必须通过哈希验证，不会改写生成成功状态。自动演示不修改冻结提示词或正式产物。

## 评审与导出

默认人工检查题面、两侧产物、原轨迹和视频，填写有效性、交付评分/描述、GSB 和理由后锁定。锁定绑定题面、初始/结束快照、Session 和原轨迹哈希；证据变化后需重新核验。

独立授权的辅助评价见 [授权评价](AUTHORIZED_REVIEW.md)。它可以形成评分和理由草稿，但保留 AI 来源，不能填写人工有效性、人工无 AI 声明或替人锁定。不得把此流程当作无需授权的默认自动评审。

严格导出保持 **26 列、无表头**：题目与环境、基线、A/B Session/轨迹/提交/视频、两侧完整性评分及描述、GSB、内部质检/反馈/备注。有效性在交付台把关，不额外插入列。原轨迹缺失、真实额外 Agent 派发、冻结起点不符或明确模型不匹配会阻止有效交付。机械通过不等于完整语义质审。

OSS 上传需要自行配置 endpoint、region、bucket、凭据与可访问链接；没有可用的预置个人 bucket。上传、创建仓库、真实生成等是显式操作。分享前检查轨迹和视频；历史证据不自动删除，更新与备份时需保留任务数据。

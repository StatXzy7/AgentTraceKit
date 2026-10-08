# AgentTraceKit

把 Codex CLI / Claude Code 会话变成可验证的轨迹包，并通过浏览器运行、复核和导出同题 A/B 任务。

[English](README.md) · [可用性与改进说明](docs/USABILITY_AND_IMPROVEMENTS.md) · [完整版本逻辑](docs/ARCHITECTURE.md) · [更新日志](CHANGELOG.md)

## 安装当前 main

需要 Python 3.10+ 和 Git；继承 Codex TOML 配置建议使用 Python 3.11+。真实生成还需安装选定的 CLI 并配置认证；自动建 GitHub 仓库需 `gh auth login`。

```powershell
git clone https://github.com/StatXzy7/AgentTraceKit.git
cd AgentTraceKit
python -m pip install -e .
atk doctor
atk browse
```

以上安装仓库中的最新版本。同步 GitHub 不代表已经发布新版 PyPI 包。独立安装也可使用 `uv tool install git+https://github.com/StatXzy7/AgentTraceKit.git@main`。

采集器默认只读本地文件，不上传：`atk collect --latest` 选择最新 Codex 会话，`atk collect --input FILE` 指定文件，`atk verify BUNDLE` 核对哈希与证据引用，`atk view BUNDLE` 打开浏览器复核界面。`atk demo --synthetic` 无需模型和凭据即可验证采集与导出链路。原始轨迹按字节保留，分享前自行检查内容。

## Pair 交付台

```powershell
python -m agent_trace_kit.desk
python -m agent_trace_kit.desk status
python -m agent_trace_kit.desk stop
```

浏览器入口：<http://127.0.0.1:8765/>。Windows 可双击 `scripts/windows/start-desk.vbs`；Linux 可前台运行或配置 systemd。关闭浏览器不会停止后台任务。

1. 配置 Codex / Claude 连接、模型与运行预算。两种 CLI 连接独立；每条 Pair 固定连接版本与模型。
2. 选择不含子模块的本地独立 Git clone、已有 GitHub commit 或自动新建仓库。A/B 和重跑从冻结的完整 SHA 开始，产物使用独立分支。
3. 导入或创建任务，运行 A/B，查看实时日志与逐次尝试记录。失败保留证据，缺失完成标记不会标成成功。
4. 在 Windows 录屏，或在 Linux 独立演示副本中执行配置的浏览器/终端操作并录屏。视频硬上限 **89 秒**。
5. 复核产物、原轨迹、视频与完整性，填写有效性、评分、GSB 和理由，锁定后导出原有 **26 列 TSV**。

默认由人完成评审。独立授权的 AI 辅助评价流程保留明确来源，不能替人填写有效性或“未使用 AI”的声明。自动演示完成仅证明指定步骤完成，不能替代完整产品验收。

数据默认位于 Windows 的 `C:/AgentTraceKit-data/desk/` 或 Linux 的 `~/.local/share/agenttracekit/desk/`，可用 `ATK_DESK_HOME` 指定。密钥不回显；Windows 使用 DPAPI，Linux 使用私有权限文件。配置隔离和进程清理不等于操作系统级 A/B 隔离。

操作与限制见 [Pair 交付台](docs/PAIR_DESK.md)、[单 Agent 保护](docs/COMPLIANCE_GUARD.md)、[Linux 部署](docs/LINUX_DEPLOYMENT.md)、[自动演示](docs/LINUX_DEMO.md)。

## 开发验证

```powershell
python -m pip install -e . pytest
python -m pytest -q tests
```

CI 使用 Python 3.11 在 Windows / Ubuntu 运行框架测试；合成测试不证明真实模型接口在线，也不等于人工桌面验收。具体验证记录和改进方向见 [可用性与改进说明](docs/USABILITY_AND_IMPROVEMENTS.md)。

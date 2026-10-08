# Linux 部署与维护

此文使用通用路径和占位 SSH 主机，不包含任何现有生产服务器身份。源码更新不执行部署。

## 最小运行

```sh
git clone https://github.com/StatXzy7/AgentTraceKit.git
cd AgentTraceKit
python3 -m venv .venv
.venv/bin/python -m pip install -e .
export ATK_DESK_HOME="$HOME/.local/share/agenttracekit/desk"
.venv/bin/python -m agent_trace_kit.desk foreground --no-browser
```

安装 Git 以及需要的 CLI；在运行服务的同一账号配置认证或在工作台设置专用连接。完整继承 Codex TOML 配置使用 Python 3.11+。默认仅监听 `127.0.0.1:8765`。

客户端通过已有 SSH 配置转发（将 `atk-server` 换成自己的主机别名）：

```sh
ssh -N -T -o ExitOnForwardFailure=yes -L 127.0.0.1:18765:127.0.0.1:8765 atk-server
```

浏览器访问 <http://127.0.0.1:18765/>。客户端退出不影响已经由后台服务运行的任务；前台 SSH 启动进程的生命周期取决于会话管理方式。

## 专用运行时与 systemd

`scripts/linux/prepare-host.sh` 是 Alibaba Cloud Linux 3 / dnf 的专用运行时安装脚本，需管理员执行；包含固定 CLI/Node/FFmpeg 版本并验证下载校验和。它不是任意 Linux 发行版通用安装器。`prepare-desktop.sh` 安装 X11 桌面与 Playwright Chromium，供真实演示使用。运行前审阅版本、路径和依赖。

提供的 service 模板假设：运行账号 `atk`、源码 `/opt/agenttracekit/current`、venv `/opt/agenttracekit/venv`、数据 `/var/lib/agenttracekit/desk`、运行时 `/opt/agenttracekit/runtime`。按部署修改 User、Group、PATH、WorkingDirectory、资源限制和写权限。桌面可选；未安装桌面时去掉任务服务的桌面依赖。两个模板中的 `PrivateTmp=no` 用于共享本机 X11 socket。

只在回环监听工作台、VNC 和 noVNC，通过 SSH 转发；不要把无认证工作台直接暴露公网。Key 文件与授权登记保持私有。任务账号不得写管理员的独立模型授权登记。

## 升级、验证与回滚

1. 确认任务、录屏及后处理全部空闲，备份数据目录、私有配置和旧 release。
2. 将指定源码提交安装在新 release，在独立环境运行 `python -m pytest -q tests`。
3. 切换 `current` 后重启服务，检查回环健康、日志和指定 CLI 版本；再做经授权的业务验证。
4. 若失败，切回旧 release/环境，保留新失败记录；不要回写或删改历史轨迹与评分。

```sh
systemctl status agenttracekit agenttracekit-desktop --no-pager
journalctl -u agenttracekit -n 80 --no-pager
df -h
free -h
```

service 模板资源限制不等于并发建议。默认先使用少量 Pair，根据运行内存、编译负载、桌面和供应商配额调整。历史工作区和录像不会自动清理。框架隔离配置与进程组，但不提供 A/B 之间的独立操作系统安全边界。

自动演示见 [LINUX_DEMO.md](LINUX_DEMO.md)，请求退避与恢复见 [COMPLETION_PROFILE.md](COMPLETION_PROFILE.md)。

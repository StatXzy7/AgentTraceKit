# Linux 自动演示、录屏与结果复核

先按 [Linux 部署](LINUX_DEPLOYMENT.md) 安装桌面与服务。通过 SSH 转发工作台端口；如启用 noVNC，可另转发 `127.0.0.1:6080`。将 SSH 主机替换为自己的配置。

## 运行过程

1. 在工作台创建或批量导入任务，A/B 在 Linux 执行并采集轨迹。
2. 两侧运行结束后，已完成侧自动进入演示队列。同一桌面一次只演示一侧，防止 A/B 串屏；生成并发由部署设置决定。
3. 取对应提交的 `git archive`，解压到独立演示目录；不修改原 A/B 工作区和冻结提示词。
4. 按配置安装依赖，在服务器 Xvfb 虚拟桌面启动真实终端和可见 Chromium。FFmpeg 通过 `x11grab` 采集整个桌面，720p、10 fps、最长 89 秒。
5. 可见浏览器执行配置的点击、填写、滚动和断言；终端程序执行真实命令。结束后停止录屏，清理本次产物的进程。
6. 视频绑定回对应 A/B。工作台可播放视频、查看报告、下载结果 ZIP；本地不用运行产物或录屏。

### 89 秒硬上限与阅读节奏

所有录制最长 89 秒，不得通过设置或接口延长。设置读写和录制器都会限制超大参数，FFmpeg 使用 `-t` 强制结束；超过 89 秒的录制文件不作为成功结果绑定。

终端展示先显示真实命令，默认停留 5 秒后执行；真实输出同时完整保存为 `terminal-output.log`，画面默认每秒约 40 个字符，每页停留 6 秒，最终结果停留 12 秒。短演示尽量保留 30 秒。浏览器滚动采用小步缓速移动。所有停留均包含在 89 秒内。

若完整输出和阅读停留无法装进时限，演示必须标记不完整并保留原始日志，不能把截断录屏标为完整，也不能加速到无法阅读来假装满足要求。命令执行结果与画面展示完整性分别记录。

本机断网/关机不影响这一过程。录屏期间请勿操作服务器桌面；默认桌面链接为只读观看。空闲时桌面没有打开的产物窗口，这是正常状态。
手动 SSH 操作、重新演示仍可用。失败不会自动无限重录，可修正演示配置后点「重新自动演示」。

## 支持的产物

- 静态 `index.html` 或 `public/index.html`：自动启动 HTTP 服务，加载、滚动并录屏。
- 包含 Vite / Next.js 依赖和 `dev` 脚本的 Node 项目：自动安装依赖并使用独立端口启动。
- 其他网页、Python CLI、测试程序等：在产物仓库**提交** `atk-demo.json`，指定启动和演示步骤。

静态页面的默认加载/滚动只证明能打开，不证明所有功能正确。功能验证应提供明确步骤与断言。
没有可识别入口、缺依赖、启动超时、断言失败、浏览器异常或录制失败，都会显示「演示失败」，保留日志和可用视频。
原生成任务本身失败时不自动演示基线；如需查看失败产物，可主动点重新演示。
Windows 专属 GUI、GPU 程序、需要外部数据库或专有服务的产物，仍需其 Linux 运行环境；不会伪造成功演示。

## 网页演示配置示例

```json
{
  "kind": "web",
  "setup": [],
  "start": ["{python}", "-m", "http.server", "{port}", "--bind", "127.0.0.1"],
  "path": "/",
  "steps": [
    {"action": "assert_visible", "selector": "#counter"},
    {"action": "click", "selector": "#increment"},
    {"action": "assert_text", "selector": "#counter", "text": "1"},
    {"action": "fill", "selector": "#name", "value": "测试用户"},
    {"action": "press", "selector": "#name", "key": "Enter"},
    {"action": "scroll", "y": 400},
    {"action": "wait", "seconds": 2}
  ]
}
```

命令采用参数数组，不是 PowerShell/Bash 命令字符串。`{port}` 替换为独立空闲端口，`{python}` 替换为服务 Python。
网页服务必须监听 `127.0.0.1` 和分配的端口；不会复用已有监听。`path` 只能是本服务路径。
`setup` 最多 8 条，每条最多 300 秒；演示最多 30 步、每个操作最多约 5 秒，总录制受 89 秒限制。
依赖安装在录制前进行，输出保存在 `setup.log`；视频记录真实启动与产品演示。

终端程序：

```json
{"kind":"terminal","start":["{python}","demo.py"],"setup":[]}
```

程序 stdout/stderr 会显示在服务器 xterm 中，同时写入 `terminal-output.log`。非零退出和超时均为失败。
题目提示词禁止添加部署、迁移或录屏要求。演示入口由执行方在产物生成后、独立副本中配置，并单独记录变更；不得写入题目、追加续接消息或改写冻结产物。当前自动识别不足时停止并人工配置，不能要求生成模型补写演示入口。

## 结果位置和含义

- 每个任务：`/var/lib/agenttracekit/desk/evidence/<pair-id>/`
- 当前 A/B 视频：`a-video.mp4`、`b-video.mp4`
- 每次演示：`demo/a/<run-id>/` 和 `demo/b/<run-id>/`，包括 `report.json`、实际 `recipe.json`、日志、视频副本、网页截图。
- 演示副本：`/var/lib/agenttracekit/desk/demo-workspaces/<pair-id>/<side>/<run-id>/`
- ZIP：任务记录、该任务证据和 SHA-256 清单；不包含全局配置、凭据、其他任务或本地历史。

`demo.status=ready` 表示配置的演示步骤执行完成，`provenance=automated-linux-x11` 明确来源，`human_reviewed=false` 表示尚待你复核。它不等于人工 GSB 结论或完整产品合格认证。
GSB 与交付评分在工作台填写并保存在 Linux；既有人工评审确认字段不会由自动演示代填。确认、上传 OSS 和 TSV 导出沿用原工作台流程。

无桌面浏览器时也可复制：

```powershell
scp -r atk-server:/var/lib/agenttracekit/desk/evidence/<pair-id> .
```

演示工作区、重复视频与日志会占磁盘；当前不自动删除历史证据。批量运行前检查空间。

## 维护

```bash
systemctl status agenttracekit agenttracekit-desktop --no-pager
journalctl -u agenttracekit-desktop -n 60 --no-pager
```

配置开关：`/var/lib/agenttracekit/desk/settings.json` 的 `linux_auto_demo`（默认 false，配置桌面后显式开启）。
桌面和 VNC/网页只监听本机，X11 禁用 TCP 并使用 Xauthority；公网无需开放 5900、6080、8765。
部署脚本见 `scripts/linux/prepare-desktop.sh`，虚拟桌面由 `agenttracekit-desktop.service` 随开机启动。

# Troubleshooting

Use `atk doctor` for client and output checks. Use explicit absolute paths when a project contains spaces or non-ASCII characters. Re-run collection after a source file is stable; a new bundle is created rather than replacing old evidence. Upload and recording integrations are optional and report not configured when dependencies are absent.

## GSB 打开 127.0.0.1:8080 却看到 Steam / Inspectable WebContents

这不是产物失败。Windows 上 Steam 的 CEF 远程调试常占用 8080；模型若再执行 `python -m http.server 8080` 会得到 `EADDRINUSE`，此时打开该地址看到的是 Steam 调试页。交付台任务详情里的「验收端口助手」会标出占用进程并给出空闲端口启动命令。验收规则：

1. 按 README 启动，若端口被占则改用助手给出的空闲端口（不要复用已有监听，等同 Playwright `reuseExistingServer: false`）。
2. 先点「探测该地址是不是产物」：标题为 Inspectable WebContents 或含 Steam 链接则立刻换端口。
3. 含 `type="module"` 的页面必须走本地 HTTP，禁止 `file://`。
4. 不要关闭 Steam 来「腾端口」；换端口即可。


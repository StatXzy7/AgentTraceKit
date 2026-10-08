# GitHub 可选 SSH 出口转发

服务器无法直接访问 GitHub 时，可以显式通过客户端已有 HTTP 代理转发。该功能需要客户端、SSH 隧道和代理持续在线，不是所有部署必需条件。

```sh
ssh -N -T -R 127.0.0.1:18789:127.0.0.1:7890 atk-server
```

将 `atk-server` 和客户端 `7890` 改为自己的配置。只给 GitHub/Gist HTTPS Git 操作设置 URL 范围代理：

```sh
git config --global http.https://github.com/.proxy http://127.0.0.1:18789
git config --global http.https://gist.github.com/.proxy http://127.0.0.1:18789
```

`scripts/linux/gh-local-egress` 是按专用运行时路径编写的可选包装器：只给自身及子进程设置代理，再调用 `gh-direct`；部署前确认原二进制入口和路径。它不自动安装或修改模型进程的全局代理。保留正常 TLS 验证。

隧道中断时 GitHub 操作可失败；已有代码与证据仍保留，不应为补推送自动重跑模型。原任务数据、轨迹和评审不因网络补救改写。

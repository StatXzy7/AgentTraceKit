# Pair 交付台使用说明（Pair-wise GSB 自动化）

`atk desk` 是一个本地网页控制台，把「同一道题用同一模型跑 A/B 两次」的机械工作全部自动化：
批量出题 → 复制隔离工作区 → 并行跑两次 Claude Code → 自动提交并 push → 自动收轨迹 → 一键传
京东云 OSS → 逐项检查缺什么 → 导出可直接粘贴飞书的 TSV。**GSB 判断和理由仍然由你人工完成，
工具不做任何轨迹语义分析。**

## 启动

```powershell
cd D:\myprojects\AgentTraceKit
python -m agent_trace_kit.desk          # 或 atk desk
```

浏览器自动打开 http://127.0.0.1:8765 。所有数据在 `C:\AgentTraceKit-data\desk\`
（任务记录、A/B 工作区、轨迹证据、日志），不在 git 仓库内；进程关掉重开，任务状态自动恢复。

## 数据落在哪

| 内容 | 位置 |
|------|------|
| 任务/设置状态 | `C:\AgentTraceKit-data\desk\jobs\*.json`（原子写，可断点恢复） |
| A/B 工作区 | `C:\AgentTraceKit-data\desk\workspaces\<pair-id>\a|b` |
| 轨迹 jsonl、运行日志 | `C:\AgentTraceKit-data\desk\evidence\<pair-id>\` |
| 京东云密钥 | `C:\AgentTraceKit-data\desk\secrets.env`（仓库外，勿外传） |

## 设置（页面右上角 ⚙）

- **claude 命令**：默认 `claude`。子进程完整继承你当前环境——cc-switch 选的模型
  （auto_model/urm）、全局 `~/.claude/settings.json` 的网关和 1M 上下文原样生效，工具不改模型。
- **最多并行 pair 数**：默认 2（即最多同时 4 个 claude 进程）。注意供应商 Key 有并发上限。
- **单侧超时**：默认 1800 秒。
- **OSS**：endpoint/区域/bucket 已默认填好（`agenttracekit-pairwise`，cn-north-1），
  桶已配置公开读。点「测试连接」验证；误删桶后可点「创建 bucket」。

## 标准作业流

1. **准备题目仓库**（每题一个）：新建/打开一个 git 仓库，放好题目的初始代码或 PRD，
   `git init`、提交初始 commit、`git remote add origin <你的 GitHub 仓库>`（用 `gh repo create`
   或自己建私有/公开库均可）。`.gitignore` 必须排除 `.env`、密钥。
   - 初始快照要求仓库 push 到评测方可访问的 GitHub；工具准备时会自动 push 当前基线分支。
2. **新建任务**：页面「＋ 新建任务」，粘贴**完整 prompt 原文**，选题型（默认 0-1代码生成）、
   难度（只允许困难/地狱）、语言框架，填题目仓库目录。创建后工具自动：
   快照基线并 push → 复制出两个隔离工作区 → 建 `pair-<id>-a/-b` 两个分支 → 入队运行。
3. **批量出题**：「📋 批量导入提示词」，每行一条。纯文本=每行一个 prompt；
   TSV 行可带 `提示词⇥题型⇥难度⇥语言框架⇥基线仓库目录`（第 5 列可逐行指定不同题目仓库）。
4. **自动跑 A/B**：准备时每个工作区会写入本地 `.claude/settings.local.json`，包含供应商要求的
   1M 三件套（`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`、`DISABLE_COMPACT=1`、
   `ANTHROPIC_BETAS=context-1m-2025-08-07`），该文件通过 `.git/info/exclude` 保证**永不被提交**。
   网关地址、token、各档模型映射继续沿用你全局/cc-switch 已验证可用的配置（seed-code.bytedance.com，
   auto_model/urm[1M] 主模型），不写进任务仓库。随后工作区各自执行
   `claude -p --permission-mode acceptEdits --verbose "<同一 prompt>"`，只一轮；
   跑完自动 `git add -A` + commit + push 两侧分支，记录 40 位 SHA 和 permalink，
   按工作区 cwd 从 `~/.claude/projects` 精确匹配出本次会话 jsonl 并留存证据副本。
   - 起标题子请求偶发 `unrecognized_model auto_model/urm` 警告是该网关已知特性，主回复不受影响。
   - 页面每 5 秒自动刷新运行状态；单侧可「中止」「重跑」（重跑会从基线重新复制该侧，不污染另一侧）。
5. **录屏（仅有的两件人工事之一）**：分别为 A、B 侧点「● 开始录屏」（内置 ffmpeg 录主屏幕，
   无需第三方软件），切到产物窗口从干净状态展示真实运行，点「■ 停止并保存」；
   **90 秒自动停止**，失败的产物也要录。工具用 ffprobe 自动查时长，超 90 秒亮红灯。
   也可以用「选择已有文件…」绑定你用别的软件录好的 mp4。
6. **③ 上传到 OSS**：jsonl 与 mp4 传到 `pairwise/<sessionid>.jsonl|.mp4`，
   回填公开链接，并可在线核验匿名可访问。幂等，可重复点。
7. **检查清单**：题目/环境/初始快照/A/B/一致性/GSB/安全分组逐项亮灯。阻塞项全绿才能导出。
   关键机械核验包括：
   - 初始快照是 A、B 产物 commit 的**祖先**（`git merge-base --is-ancestor`）；
   - 两个 SessionID 不同；轨迹用户消息里能找到本题 prompt；只有一轮交互；
   - 40 位 SHA permalink 格式、已 push；A/B 产物 SHA 不同；
   - 难度只能是困难/地狱；理由长度（Same ≥80 字，其余 ≥30 字）。
8. **写 GSB（第二件人工事）**：在页面填结论和理由——A、B 分别说好坏，定位到具体文件/报错/步骤。
   **严禁用 AI 分析轨迹或代写理由。**
9. **⑤ 生成 TSV**：全绿后按钮可用，生成 20 列规范行，「复制 TSV」到飞书多维表格整行粘贴即可。

## 崩溃恢复

- 工具运行中直接关窗口也没关系：状态每次变更都原子落盘。下次 `atk desk` 启动时，
  上次「运行中」的侧标记为失败并说明原因，点「重跑该侧」即从基线重新复制再跑，
  已完成的另一侧不会重复执行。
- 模型自己在工作区里的改动都在独立分支并已 push，重跑不影响已有证据。

## 回滚/清理

- 某条任务不要了：删除 `C:\AgentTraceKit-data\desk\jobs\<id>.json` 及对应
  `workspaces\<id>`、`evidence\<id>` 即可；远端分支可在 GitHub 删除。
- A/B 分支名：`<pair-id>-a`、`<pair-id>-b`，基线在基线仓库原分支（通常 main）。

## 安全注意

- `secrets.env` 含京东云明文密钥，已在仓库外；对话里泄露过的 Key 建议任务跑通后到京东云轮换。
- 不要在 prompt、附件里放密码/Token/Cookie/私钥（网关与对象存储侧均可能留存）。
- 轨迹/录屏上传前自行检查是否含敏感内容；桶为公开读，任何拿到链接的人都可访问。

## 已知边界

- 录屏必须人工录制（规范要求真人运行产物），工具只负责选文件、查时长、上传。
- `acceptEdits` 允许模型自动改文件；如题目需要模型执行危险命令，请在题目仓库里用
  `.claude/settings.json` 单独配置权限策略。
- 工具只支持 Claude Code（本期范围）。

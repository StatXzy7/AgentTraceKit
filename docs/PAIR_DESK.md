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
- **基线父目录**：只填仓库名新建任务时，本地文件夹建在这里（默认
  `D:\myprojects\GoletaLab数据标注\github-base`）。
- **GitHub 归属账号**：留空 = `gh` 当前登录账号（页面显示登录状态，如 `StatXzy7`）；
  新仓库默认公开（评测方可直接访问），可改私有。需要本机 `gh auth login` 且有 `repo` 权限。

## 标准作业流

1. **新建任务时只需填一个仓库名**（每题一个 GitHub 仓库，本地文件夹同名）：
   页面「＋ 新建任务」，粘贴**完整 prompt 原文**，选题型、难度、语言框架，
   填 **GitHub 仓库名**（字母数字 `- _ .`）和一句 **README 说明**（可留空）。
   点创建后工具全自动完成：
   - 校验仓库名（GitHub 规则 + Windows 保留名），并用 `gh` 探测远端是否已存在；
   - 远端不存在 → `gh repo create` 在当前登录账号下建空仓（默认**公开**，可选私有）；
   - 在后台设置的「基线父目录」（默认 `D:\myprojects\GoletaLab数据标注\github-base`）下
     同名 clone 空仓，写入 `README.md`（`# 仓库名` + 你的说明）和跨语言 `.gitignore`，
     提交初始 commit 到 **main** 并 push；
   - 随后照常：快照基线 → 复制两个隔离工作区 → 建 `pair-<id>-a/-b` 分支 → 入队运行。
   - 仓库名输入框会实时探测：名称非法、`gh` 未登录、远端已存在（直接复用，不重建）都有提示。
   - 远端仓库已存在但本地没有文件夹时自动 clone 使用，**绝不**注入 README 提交或 force push；
     已存在的非 git 同名文件夹会被拒绝接管，不会动用户文件。
   - 前置条件：安装并登录 GitHub CLI（`gh auth login`，需 `repo` 权限）；后台设置页有登录状态指示。
2. **使用已有题目仓库（兼容旧流程）**：展开新建面板的「高级」，直接填「已有本地基线仓库目录」即可，
   会跳过自动建仓直接快照。初始代码/PRD 需已提交并 push 到评测方可访问的 GitHub。
3. **批量出题**：「📋 批量导入提示词」，每行一条。纯文本=每行一个 prompt；
   TSV 行可带 `提示词⇥题型⇥难度⇥语言框架⇥GitHub 仓库名`（第 5 列填**仓库名**即自动建仓；
   也可填本地已有仓库的**完整路径**，含 `\` 或 `/` 会自动判别为路径而直接快照）。
4. **自动跑 A/B**：准备时每个工作区会写入本地 `.claude/settings.local.json`，包含供应商要求的
   1M 三件套（`CLAUDE_CODE_MAX_CONTEXT_TOKENS=1000000`、`DISABLE_COMPACT=1`、
   `ANTHROPIC_BETAS=context-1m-2025-08-07`），该文件通过 `.git/info/exclude` 保证**永不被提交**。
   网关地址、token、各档模型映射继续沿用你全局/cc-switch 已验证可用的配置（seed-code.bytedance.com，
   auto_model/urm[1M] 主模型），不写进任务仓库。随后工作区各自执行
   `claude -p --permission-mode bypassPermissions --verbose "<同一 prompt>"`，只一轮；
   跑完自动 `git add -A` + commit + push 两侧分支，记录 40 位 SHA 和 permalink，
   按工作区 cwd 从 `~/.claude/projects` 精确匹配出本次会话 jsonl 并留存证据副本。
   - 起标题子请求偶发 `unrecognized_model auto_model/urm` 警告是该网关已知特性，主回复不受影响。
   - 页面每 5 秒自动刷新运行状态；单侧可「中止」「重跑」（重跑会从基线重新复制该侧，不污染另一侧）。
5. **录屏（仅有的两件人工事之一）**：分别为 A、B 侧点「● 开始录屏」（内置 ffmpeg 录主屏幕，
   无需第三方软件），切到产物窗口从干净状态展示真实运行，产物运行结束后手动点「■ 停止并保存」；
   无时长上限，失败的产物也要录。工具用 ffprobe 自动查时长回填。
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
- 默认 `--permission-mode bypassPermissions`：`-p` 非交互下 `acceptEdits` 只放行文件编辑，
  Bash 会被全部自动拒绝（曾致一轮 35 次 python/pytest 调用被拦、产物零验证）。工作区是
  一次性基线副本，可整体丢弃，故默认放行全部工具；如需收紧可在后台设置 `permission_mode`。
- 工具只支持 Claude Code（本期范围）。

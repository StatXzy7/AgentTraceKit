# 显式授权的辅助评价

默认工作台保留人工评审。批处理模块是可选工具，需要先冻结题目、提交收据、评分规则和预算，并获得实际操作者对模型评价及相关操作的授权。代码或配置文件本身不是用户对新批次的授权。

目前控制器绑定完整 **20 个任务**及生成模型 `auto_model/urm`。`batch_pipeline` 通过任务 API 后处理并留证，不续接生成会话。使用其他评价模型须由管理员在 `/etc/agenttracekit/authorized-review-models.json` 登记：schema、authorization ID、模型、生成模型、job_ids，以及 `origin=human-authorized-local-evaluation`。Linux 要求登记文件及目录由管理员持有、不可由运行账号写入。

Windows `local_review_worker` 必须显式提供 `--config FILE --root DIRECTORY`。私有 UTF-8 JSON 包含：

| 字段 | 内容 |
| --- | --- |
| `schema` | `1` |
| `model`, `generation_model` | 已授权的评价/生成模型 |
| `authorization_id` | 与管理员登记及请求完全一致的授权编号 |
| `ssh_host` | 操作者配置的 SSH 主机/账号别名 |
| `remote_batch`, `remote_evidence`, `remote_python` | Linux 的绝对批次目录、证据根目录和 Python 路径 |
| `job_ids` | 20 个不同且已授权的 Pair ID |
| `prelaunch_recoveries` | 可选；请求 ID 到经人工审计的完整恢复收据的映射，默认空 |

未配置时不接受任何任务，也不连接 SSH。只允许配置中的任务、模型、路径和期限，传输 bundle 验证大小/哈希并安全物化文件；不创建归档内 symlink 或 OS hardlink。派发意图独占落盘，未知结果不能自动再派发。特定派发前解包失败只有精确私有收据、空工作区和未派发证据匹配时才能恢复。

将配置、授权登记、SSH 凭据和真实批次收据放在 Git 之外；本仓库不附带任何生产 allowlist。该 worker 使用本地 Codex 权限绕过参数运行辅助评价，只应在已审阅的隔离评价机器上运行；此路径不提供 OS 沙箱。实际构建/探针和录屏仍应在指定 Linux 环境执行，不能把 Windows 本地运行标成 Linux 验证。

评价结果保存 AI 来源、模型、证据哈希和每次失败。有效性、人工无 AI 声明和人工锁定不能由辅助模型代填；严格导出保留来源并重新核验原件。

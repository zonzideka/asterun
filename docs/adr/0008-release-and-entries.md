# ADR 0008：发布包与入口启停

日期：2026-09-07。状态：已采纳（A4 离线）。

## 决策

发布文件只含 `src/asterun` 与文档/脚本/非代码样例。参考快照、凭据、状态库和作者路径不得进入 wheel/sdist。诊断默认脱敏。状态备份使用 SQLite backup API，恢复不改写用户工作区或原生会话。

CLI 与 MCP 是可选入口，由 `entries.cli` / `entries.mcp` 独立开关。停用只阻止该入口的新任务，不影响核心和其他入口。GrokBot 模板不迁入本仓库。

干净安装与两次 fake 任务只能证明隔离配置可复用，不能替代真实用户与真实后端验收。

## 回退

忽略 `entries` 字段；删除 diagnose/backup 命令；发布检查脚本可停用。参考快照仍只存在于仓库 `references/`，不进入默认 pytest。

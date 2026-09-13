[中文](codex-projects.md) | [English](en/codex-projects.md)

# 将 Codex 任务放入真实项目

在 Codex 后端启用 `desktop_projects: true` 后，Asterun 会按真实工作区目录登记项目，将原生线程关联到该项目，再请求 Codex Desktop 打开任务。执行主机须安装 macOS Codex 应用，并使用与后端相同的原生 HOME。

## 启用项目登记

配置 v1 的后端片段如下：

```json
{
  "kind": "codex",
  "enabled": true,
  "bin": "/absolute/path/to/codex",
  "desktop_projects": true
}
```

配置 v2 将该布尔字段放在 `connections.<ref>.options` 中。修改后按[配置修订流程](install.md#会话绑定与配置修订)重启并应用。旧配置默认关闭此功能；后端账户、工作区授权和执行开关沿用已有设置。

## 项目如何匹配

Asterun 先通过系统目录打开入口登记 Desktop 项目，再回读项目映射，通过原生 `project/read`、`thread/read` 和 `thread/metadata/update` 关联同一线程。适配器核验接口的实际响应，任务的执行目录始终保留原 `cwd`。

匹配依据是规范路径和已验证的 Git worktree 关系。已有项目直接复用；映射有歧义、归属冲突或接口缺失时，单独返回展示结果。GitHub 仓库需先在执行主机准备本地目录。

更新原生元数据前后都会回读核验。其他客户端若同时迁移该线程，仍可能发生竞争；操作期间请保持线程项目归属稳定。

## 查看状态和重试

`task-get` 的 `run.native.desktop_project` 分别记录 `registration`、`association` 和 `client_visibility`。`desktop` 保存项目回执及打开线程的请求结果。若 `client_visibility` 为 `not_verified`，桌面显示仍需实际确认。

后台执行会先保存线程和受理轮次，再尝试登记项目，展示报告单独保存。若出现 `receipt_persistence: unknown` 或报告缺失，用原任务重试回读：

```sh
asterun --state-dir /path/to/state --connect task-present TASK_ID
```

MCP 使用 `task_present`，参数为 `{"task_id":"TASK_ID"}`。返回的 `presentation` 描述展示结果，`re_dispatched: false` 表示继续使用原执行记录。任务仍在本机观察时，先等待自动登记；重试会保留原账户、配置修订、工作区和原生身份绑定。

登记前，Asterun 在 `~/.local/share/asterun/codex-desktop-intents` 保存未决意图，同一用户的各实例共用此目录。结果不确定时继续回读，避免重复登记。若系统未处理最初的打开请求，可在 Codex Desktop 中手动打开真实目录，再执行 `task-present`。确认回执后，Asterun 会清除对应意图；备份时需在核心数据库之外保留该目录。

项目和会话保存在实际执行主机。不同电脑上的同名项目分别保存各自的原生历史。

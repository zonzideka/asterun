# 外部主控与同任务修复

安装当前 Asterun 后运行 `python examples/caller-workflow/demo.py`。全部数据保存在自动清理的临时目录；只启动 fake 后端，不调用模型。示例通过真实 CLI 完成提交、固定快照、外部检查失败、同任务修复、幂等重放、复验，以及修改文件后旧报告失效。

fake 后端不会修改文件，示例显式写入夹具模拟修复产物。生产调用方应读取实施者实际生成的文件，用 `workflow-snapshot` 固定版本，再运行检查。不要另建任务规避修复上限，也不要把 `run.status=succeeded` 当成验收。

局部编译和测试可使用配置固定的 `workflow-check` / MCP `workflow_check`，返回的 `report` 可保存到工作区，用其 SHA-256 经 `workflow-evaluate` 导入。失败的文件、错误码和摘要会进入 `workflow-repair` 提示。完整参数及受控检查配置见[调用方手册](../../docs/caller-orchestration.md)。

测试数量必须来自运行器。配置 `result_file` 时需提供 `passed`、`failed`、`skipped`、`load_errors` 四个非负整数；即使进程退出 0，只要存在加载错误或没有实际通过的测试，也不能通过。自测报告不能替代独立审查。

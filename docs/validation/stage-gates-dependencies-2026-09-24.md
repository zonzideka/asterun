# 阶段门禁、依赖队列与只读导入验证

基线为 `0ce86a9`，本批分支 `codex/workflow-stage-gates-0924`。因原工作区有另一维护任务活动，本批在独立工作树实施。配置 v1/v2、SQLite schema 6 和 runner-control/v1 未改变；旧配置默认行为保持兼容，新增配置和任务字段均可选。本记录只涉及离线替身、临时 Git 仓库与 macOS 本地沙箱，不是生产或真实模型验收。

A05 验证覆盖必需消费者缺项、删除配置/检查名/省略 checks 不能绕过、独立审查 simulated 不满足真实门禁、外部回执不能降低 core 要求、12 项加载错误而 failed=0 不能通过、报告篡改/删除及源码漂移、SQLite 重开、CLI/MCP 参数和权限。外部成功不能盖住本地失败。核心质量流程只审查一次，缺阶段等待补证；完成后回执删除使验收失效，恢复同版本报告后仍不重复调用审查。报告读取后再次核对目标，来源和 acceptance 分开记录。

A06 验证覆盖上游仅执行成功时下游等待、非就绪队列项不阻塞独立任务、上游验收通过后放行、父源码漂移使子验收失效、依赖参与幂等、SQLite 队列重开、同/嵌套工作区拒绝、两 worker 通过但组合检查失败、上游已放行而同池 402 后零新增派发。独立 Git 夹具覆盖固定提交创建、幂等复用、源 dirty 不复制、目标 dirty 不覆盖、非空目标/活动别名/checkout 过滤器/权限拒绝。运行总限额跨后端生效，检查限额单独生效；默认仍为每后端并发与两个本地检查槽位。

A07 只读导入测试覆盖规范 UUID/工作区/日志 SHA 绑定、白名单用量投影、首次/重复/跨任务导入、日志漂移、符号链接、来源绑定不明、权限、SQLite 重开及 CLI/MCP。重复导入不增加模型调用、运行或 usage-report 总计；acceptance 不变，不创建原生 marker。没有实施直调会话接管；没有活动 PID 的可信归属和独占权交接证据时继续拒绝受管 resume，不把日志可读等同于可接管。

A08 汇总器覆盖 6 项任务的 18 行模拟配对、条件错配、重复/缺失路径、无效计数、质量失败和缺失用量。真实 CLI 对模拟输入产出报告，节省比例均为 null。数值测试中的比例仅为算术夹具；本批没有运行完整三路径 fake 执行实验或真实模型对照，不能给出净节省结论或新的 Astra 模式实测推荐。

清洁环境命令 `ASTERUN_VERIFY_TMP=/tmp/asterun-stage-gates-0924 PYTEST_ADDOPTS='-o addopts= -ra' bash scripts/verify-offline.sh` 完成：Python 3.14.0，2422 passed、4 skipped，261.42 秒。跳过的是 3 个 Codex 与 1 个 Grok 真实后端测试；Antigravity 三份固定来源文件核验通过。全量运行启动后的收敛包括 Git 输出流限额、阶段报告后目标复核、已完成审查的同版本补证，以及三个新增边界用例，均在最终源码专项覆盖，不并入上述全量数量。

最终源码专项命令为 `python -m pytest tests/test_stage_gates.py tests/test_dependencies.py tests/test_worktrees.py tests/test_session_import.py tests/test_scheduler.py tests/test_check_runtime.py tests/test_workflow_evidence.py tests/test_quality_runtime.py tests/test_observe_compact.py tests/test_workflow_samples.py -o addopts='' -q`，清洁环境下 116 项通过。打包后将测试和开发用样本汇总脚本复制到不含 src 的临时目录，在独立安装 wheel 的虚拟环境中以 `-W error` 运行同组 116 项，全通过；导入路径确认来自 site-packages。用量样本脚本是开发实验工具，不声称它属于 wheel 运行模块。

`scripts/check-release-package.py` 返回 `ok=true, problems=[]`，检查 wheel/sdist 内容、文档链接及运行源码字节。固定 wheel SHA256 为 `9e26dc799a742f3a620815a5337901f6a408d59932027764e650fed91c22e9cf`，sdist SHA256 为 `ce66d0c1ad2e2aaf3aaa751cae887234e87e40c1cfe251d9f6aa90bad4ccd8dd`。构建后仅更新状态、设计和本验证记录；这些文件不纳入发行包，运行文件与已验收 wheel 一致。

`scripts/verify-installed-control.py --venv /tmp/asterun-stage-gates-0924/installed-venv --work-dir /tmp/asterun-stage-gates-0924/control --output /tmp/asterun-stage-gates-0924/installed-control.json` 的 fake 验收通过：CLI/MCP 两任务、94 条 schema 记录、重启幂等重放和备份恢复均成功，备份 schema 6。原生取消、续接、客户端、真实模型响应与供应商身份没有因此变为已验证。

日志与私有临时产物位于 `/tmp/asterun-stage-gates-0924/`，全量日志为 `/tmp/asterun-stage-gates-0924.log`；未入 Git 或发行包。没有修改其它项目或 Nowdex，没有运行真实模型、推送、合并或切换实例。A04 连续崩溃快照/自动 WIP、完整项目/Linux 现场、直调会话接管和有预算的真实样本仍是后续工作。

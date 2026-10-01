# 后台检查、检查点和分阶段证据验证

验证基线为 `06efbfc` 后的本地维护变更，分支 `codex/grok-native-workflow-0924`。本记录只涵盖离线 fake/协议进程与 macOS 本地沙箱，没有真实模型、生产状态修改或部署。

专项验证运行 `python -m pytest tests/test_check_runtime.py tests/test_local_checks.py tests/test_checkpoints.py tests/test_workflow_evidence.py tests/test_observe_compact.py -q`，最初 59 项通过，最后补齐同步/异步共享槽位、隐式运行时范围与双重 compact 后为 61 项通过。覆盖实际 CLI 客户端退出后核心空闲落盘、MCP 查询、核心重启同键重放、两槽位限制、取消与成功竞争、终态落盘失败转 unknown、源码漂移、运行时依赖修改后缓存失效、缺失运行时禁用缓存、权限和请求 schema。缓存使用可完整枚举的小型运行时夹具，不宣称本机完整系统目录可命中缓存。

检查点专项使用 SQLite 与真实 Git 临时仓库：用户原有 dirty 内容留作基线，新增文件和实现差异保存，Git 索引与 HEAD 不变；状态归档备份恢复后摘要及产物内容一致；派发前磁盘失败零派发；执行完成但终态检查点落盘失败，重开核心保留 pending_reconcile、不再次派发。当前只保证显式选定文件的最后成功检查点，未实现全工作树或连续崩溃快照。

分阶段验证包含“单测绿但消费者未验”“12 项加载错误而 failed=0”“源码变化”“检查命令变化”“external_reported 不冒充独立审查”。分阶段汇总保持 acceptance 不变，部署阶段仍未验；调用方应将需要的消费者/组合报告显式纳入原有 evaluate 检查列表。

首次扩展专项暴露测试夹具的 SQLite 连接未显式关闭（Python 3.14 ResourceWarning）；修正夹具生命周期后，上述 59 项通过，不忽略警告。检查重启夹具曾漏复制配置 revision 与会话，已补完整绑定后验证；这些是夹具修正，不作为产品失败遗漏。

清洁环境运行 `ASTERUN_VERIFY_TMP=/tmp/asterun-checkpoints-0924.f8ghdN bash scripts/verify-offline.sh` 成功：2390 项通过、4 项真实后端测试跳过，Python 3.14.0。Antigravity 三份固定来源文件核验通过。全量回归启动后收敛的缓存实现字节绑定、共享槽位、隐式运行时范围和双重投影改动，另在最终源树执行上述 61 项专项，全部通过；未把新增的两项计入先前全量数量。

最终源码的 wheel/sdist 内容、链接与运行文件字节检查通过，`scripts/check-release-package.py` 返回 `ok=true, problems=[]`。构建后在独立虚拟环境安装 wheel，将测试复制到不含源码的临时目录运行 61 项专项（含实际 CLI/MCP 客户端退出、后台落盘和重启）；`-W error` 下全部通过。安装路径确认来自独立环境的 site-packages。最终 wheel SHA256：`fedfd5565fc0c04d2e0db44af7a2c30cf3fcdd1ad37828e85eb43e1559b4f214`。

安装后的 `scripts/verify-installed-control.py` fake 验收通过：CLI/MCP 两个任务，94 条 schema 记录，重启幂等重放与备份恢复均为 true，SQLite schema 6。原生取消、续接、客户端、真实账户与模型验证仍为 false/unknown，不据此推导现场成功。

日志目录 `/tmp/asterun-checkpoints-0924.f8ghdN/` 包含 `offline.log`、`targeted-final.log`、`package-check-final.log`、`installed-features.log`、`installed-control.json` 和 `dist/`，不纳入发布包或 Git。源码已完成本地维护验收；没有推送、合并或实例切换。

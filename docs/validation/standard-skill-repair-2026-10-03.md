# 标准 skill 修复与本机更新记录

2026-10-03，用户反馈公开 `0.1.0a13` 能提交任务，但等待报 `unrecognized arguments: --compact --no-events`，快照报 `invalid choice: workflow-snapshot`。原因是标准 skill 依赖标签之后才合入的接口。修复基于 PR #6 的 `197b18c`，实现提交为 `d837dff49b5cf790bdee041d849fef1f7fcbf304`，保留其 a14 发行准备，不创建公开标签或上传发行包。

标准发行版最低为 a14；公开标签 a13 在业务提交前拒绝。仅对仍准确报告 `0.1.0a13` 的历史源码构建检查 CLI 参数和应答核心接口，核心必须返回绑定随机不存在任务的 `NOT_FOUND` 才通过。每次重验，不缓存服务版本，不提供跳过门禁开关。连接失败、未知版本、能力不确定与确定不兼容分开报告。探测不会新建任务；部分既有读取接口仍执行核心原有 poll，不能将其称为冻结整个调度器。

技能新增 `resources` 读取授权工作区名称与路径，发现摘要保留后端执行模式，明确 fake 仅模拟。用量支持明细、会话观察与游标分页，总计仍覆盖整个查询，未知不能视为零。已复现单行 IPC 小于 1 MiB、CLI 缩进后大于 1 MiB 的合法响应被旧技能误报损坏；inspect 改为独立 4 MiB 解析限额，超限保留原文并返回明确错误。核心强制投影时显示 `response_compact`。拒绝 `--input -`，避免关闭 stdin 后提交空输入。

真实 CLI 与隔离 fake 常驻核心的五种组合符合预期：公开 a13 双端拒绝；a14 CLI 加公开 a13 核心拒绝；历史 8f3905e/a13 双端通过；a14 CLI 加历史 a13 核心通过；a14 双端通过。新增兼容专项 22 项通过。独立消费者从复制的技能完成 14 步短诗场景，实际检查两轮均没有 poem.txt，验收均为 failed；同任务 1 次修复、1 task/2 runs，来源保留 external_reported。该实验没有申请第二次修复，不以它单独证明预算拒绝。消费者所测脚本与最终候选的差别仅是 CLI 能力错误中展示实际客户端版本；最终代码另由兼容专项和完整回归覆盖。

完整清洁离线回归结果为 2610 通过、4 个真实后端跳过、1 个版本探测超时失败。失败文件确认在读取 CLI version 的 3 秒时限内未返回，业务等待请求尚未发出；不能称本次整套命令全绿。相同待审批场景随后在隔离环境连续 3 次复测通过，未放宽门禁或跳过断言。测试期间没有新增真实模型任务。独立 wheel 环境的提交/幂等/等待与验收/修复两项端到端测试通过，实际核心模块来自 wheel-runtime。标准技能格式与 Antigravity 三文件固定来源核验通过。

固定源码构建的 wheel/sdist 内容、运行文件及文档链接检查通过；从 sdist 重建 wheel 的 102 个运行文件一致，5 个技能成员也一致。技能 ZIP SHA256 为 `457334e4a26fd99b9ab8cb72dde541fb0ede73b17bfec8e0d8b543b61ba76b65`；a14 wheel SHA256 为 `ca47851bbe2b26f2bed5985ced65a0675070181ca135e643945d060e4e3ce06e`。这些是本机候选包，不是公开发布。

本机技能安装从该 ZIP 更新，清单绑定实现提交并逐文件复核，旧技能原件与副本均已保留。安装后的 discover/resources 通过，返回历史 a13 能力兼容模式，发现 4 个后端和 5 个工作区。核心仍为 8f3905e；安装前后核心 PID、入口、LaunchAgent 和配置摘要一致，没有重启正在工作的实例，也没有新建生产任务。真实模型产出、原生客户端自动触发、其它宿主与 Token 收益仍需各自验收。

私有证据保存在本机 Asterun 数据目录的 `skill-updates/20261003-compatibility`：`package-check.json`、`offline-tests.xml`、原超时证据与 `wait-recheck.json`、`wheel-tests.xml`、`sdist-rebuild.json`、`compatibility-matrix/results.json`、独立消费者记录和 `installation-result.json`。恢复技能可使用该目录保存的旧技能原件；不需要恢复或覆盖核心任务数据库。

# 输入 Token 优化实施状态

审阅锚点：84ec165。本机基线：cef1cbc；R0 基线记录提交：e902669；实施分支：codex/input-token-optimization-r0-r3。依据用户提供的 v1.1 复核计划，第一轮 R0—R3 已完成，停止在离线交付边界。新增投递/返回策略默认关闭，没有修改生产配置、启动生产实例、迁移生产数据库、升级原生客户端、调用真实模型、push 或部署。

## 变更与能力覆盖

R0：完成本机入口、已有能力与缺口映射，见 [BASELINE.md](BASELINE.md)。能力表复用 builtin capability 声明，附适配器文件 SHA 与 profile 摘要。verified 仅限代码可控边界和离线证据；真实客户端版本与内部逐模型请求仍为 unknown。没有创建 Model Gateway、平行任务账本或通用 Context Compiler。

R1：复用 usage.report，新增可选 include_observations。原生累计通知只保存白名单计数、最大观察值、最后值、采集版本/时间和乱序疑点；没有可信 epoch/顺序/基线时不求差、不归属到新 run。多个任务共享会话只保留未归属观察；账户/运行身份不完整时不跨任务合并。历史导入保持独立来源，不再次入总量，未知发生时间单列。失败/重试的已知 run 计数保留；旧分页三集合兼容，启用观察时增加第四集合且仍受原字节限额约束。R1 正确性元数据不依赖降耗开关，不增加原始消息保留。

R2：在实际投递与 task.get 返回正文上记录轻量 manifest（字节数、SHA、策略版本、UTF-8 字节数除以四向上取整的粗估）。显式材料按路径、行范围和版本选择，只消除同包同来源重复项；原 Task.text 和用户限制完整保留。observe 保持投递正文不变；enforce 超过明确字节上限就阻塞。材料每次重新核对，不建立黑盒上下文驻留假设。返回复用 compact；日志复用现有流式 8000 字节尾部和最多 3000 字符诊断，增加精确截断元数据及有界范围取回。错误分组复用 workflow_evidence，摘要用既有脱敏器生成，不替换原证据。manifest 不在普通任务响应中递归回显；返回观察元数据使用事务内更新，避免覆盖并发运行状态。

R3：复用 Task/Run/事件/审批、snapshot 和 checkpoint blob，增加带 checksum/schema/revision 的派生交接视图。SQLite 同一事务写 blob 与 CAS 指针；写入中断回滚，不返回虚假引用。读取前后复核当前状态和文件版本，并复核外部验收、质量流程、阶段及依赖证据。重建从权威记录加载完整目标、审查条件、验收检查、全部 finding、依赖及修复/运行上限。摘要中的 64 条 finding 只是预览，不裁剪重建材料。审批、待对账运行保持未决；没有启动新原生会话。

接口与配置示例集中在 [调用方接口](../caller-orchestration.md#实验性输入优化与离线交接)。

## 实际验证

隔离全量命令：`ASTERUN_VERIFY_TMP=/tmp/asterun-token-opt-0924 bash scripts/verify-offline.sh`。结果为 2472 项通过、4 项真实后端跳过；无测试失败。脚本清空凭据环境并使用临时 HOME/状态目录，Antigravity 三份固定源码校验通过。依赖安装有一次 TLS 重试，随后成功。日志：`/tmp/asterun-token-opt-offline.log`。

全量期间/之后完成交接权威字段、旧采集版本 unknown、空观察不得伪造时间的收尾及额外边界用例；最终源码四个新增测试文件合计 54 项通过：`python -m pytest tests/test_input_optimization.py tests/test_handoff_snapshot.py tests/test_usage_observations.py tests/test_input_payload_replay.py -o addopts='' -q`。早期专项 152 项通过；空观察兼容修复后，用量专项 27 项通过。过程中出现过测试文件名/夹具属性写错以及投影误删内存 manifest 的失败；均修正后复测，没有删除失败用例或修改验收标准。最终专项日志：`/tmp/asterun-token-opt-final-targeted.log`。

发行检查 `python scripts/check-release-package.py` 通过，wheel/sdist 无禁入内容或文档链接错误。最终固定包再次核对归档内运行代码与源码字节一致。独立 wheel 安装在 `/tmp/asterun-token-opt-installed`，复制测试到无源码的临时目录并以 clean env 运行，144 项相关用例通过；实际模块来自该 venv 的 site-packages。日志：`/tmp/asterun-token-opt-wheel-tests-final.log`。

安装后 `scripts/verify-installed-control.py` 使用 fake 完成 CLI/MCP 两任务、94 条 schema 记录、重启重放和备份恢复，SQLite schema 保持 6。记录：`/tmp/asterun-token-opt-control.json`。另对新增接口以真实 CLI 子进程创建交接，再通过另一 CLI/MCP 进程读取和重建；原始限制、manifest 和 revision 保持，记录：`/tmp/asterun-token-opt-new-cli/result.json`。这些都不是原生模型或生产验收。

安全覆盖包括：观察不改变文本；Mandatory 超限无派发；版本漂移、越界路径、符号链接、权限撤销、落盘失败阻塞；不同来源正文不合并；文件未变仍重新读取；受控日志尾部分页与派生摘要脱敏；累计 100/150/150、乱序/重置/重启、导入和实时并存、多任务共享、未知/零及真实重试；双 SQLite 连接 CAS、写 blob 前/后故障回滚、备份恢复、引用丢失、未来 schema 拒绝、读取途中状态变化、外部报告保持 external_reported 与审查约束。既有 compact/watch/no-events 的到期和游标回归继续通过。

## 固定样本载荷比较

样本和预期先固定在 `tests/test_input_payload_replay.py`，覆盖六类合成工作流形状；不是历史真实任务，也不是统计代表性数据集。`pytest ... -s` 输出 PAYLOAD_SAMPLE，包含每个 fixture 的 SHA。以下为一次实际离线运行的序列化字节数，IDs/时间字段格式保持原样；候选均未发送给真实模型。日志：`/tmp/asterun-token-opt-targeted.txt`。

| 样本 | 投递：基线 → 候选 | task.get 正文：基线 → compact 候选 |
|---|---:|---:|
| 短任务 | 51 → 51 | 1791 → 1701 |
| 长类型诊断 | 66 → 66 | 3735 → 2681 |
| 大量测试输出 | 75 → 75 | 16274 → 2696 |
| 跨文件任务，重复附加同一材料 | 908 → 616 | 2210 → 1707 |
| 恢复与未决动作 | 376 → 376 | 1994 → 1723 |
| 保留失败证据 | 634 → 634 | 2197 → 1802 |

没有排除零收益投递样本，不以大日志样本推导整体节省。payload_estimate 为已测；native_usage、end_to_end_quality 未测；subscription_quota_effect 未知。主控 Token 未被观测，原生每轮请求不可见，因此没有完整工作流净收益或周额度节省结论。

## 未支持、风险与下一步

本轮投递变换仅对核心 v1 普通文本入口开放；v2 固定计划、typed capability、固定只读和独立审查变换拒绝。新元数据和交接接口不绕过 runner-control 管理边界。能力声明不是当前原生安装版本的现场结果；没有 request 级账本，也没有可靠 epoch 时的 run 增量推导。严格原生 Token 上限、原生工具结果发送前拦截、会话轮换/压缩均未实现。

日志范围读取只能取回既有保留尾部，完整大日志未持久保存。脱敏仅覆盖既有匹配规则，不保证识别所有秘密；原诊断仍需原权限取回。交接仅覆盖显式选择文件及权威状态，不证明未选择文件、环境或外部副作用安全。SQLite 提供持久事务；内存存储仅作离线替身，旧 JSON 文件存储拒绝原子交接。沿用已有 blob 保留/备份，没有新增 TTL 清理，活动引用不会因本策略被删除。

回滚新变换只需按既有 revision 流程将 input_optimization.mode 改回 off；不删除已保存证据，不撤销外部动作。第一轮到此交付；R4/R5 需另行确认安装版本、任务/模型/权限、验收与真实调用预算，再决定是否开展原生会话能力和小预算真实 A/B。当前代码未 push、合并、发布或部署。

## 2026-09-24 后续 R4：显式原生上下文控制

后续“继续完成后续工作”推进了 R4 的离线实施。基于 28f31d1 建立 codex/input-token-optimization-r4，继续使用独立工作树；原主工作区与生产实例未更改。下面是本轮增量，前文 R0—R3 的“尚未实现”保留为当时记录。

复用 workflow.repair 增加 native_compact 和 handoff_fresh 两个显式策略。默认关闭，仅核心 v1 常驻执行器的普通 Codex 任务开放；同步兼容入口拒绝，fake 验证核心事务。交接包、源任务/运行/事件、权限、所选文件及旧验收证据在准备和派发边界核对，原 Task 的 run/repair 计数与后继意图同事务提交。并发后继 CAS、重复幂等键和重启保持同一派发身份，不创建另一任务绕过上限。新验收从 pending 开始。

native_compact 使用原线程，先确认源终态；原生 ACK、产物完成事件、轮次完成事件和持久历史分别核对。只有全部一致才发送工作提示。handoff_fresh 创建新线程并携带完整重建材料，明确不是 resume，不删除原线程。每个副作用前持久保存阶段；未知结果、失败或取消阻止后续提示，不自动补发。prompt_started 后对账只读保存的新轮次；此前的未确认阶段不能拿旧轮次/压缩完成作成功证据。

本机只读核验 codex-cli 0.155.0-alpha.16，使用独立 HOME/CODEX_HOME 导出 schema；没有登录或模型请求。二进制 SHA256 为 2f76d9cb0acab786dbb1cbf1020e8001d0e6d4de7b3c87a5d5769a4d03480f13。ClientRequest schema SHA256 为 8e5a1b6a7103fea63a53ef96d7ab1062decbd6571701542f5a969953e23a64f5，ServerNotification 为 df70f8f8ded90d8da63c744ccfef7018223d99e918e5e0aefa0d90856a7f67cc。导出的 compact response 是空对象 ACK，完成靠 contextCompaction 产物和 turn/completed。完整四份摘要在 /tmp/asterun-context-r4-capabilities.json；记录为 installed_schema_only、live_compaction_verified=false。实际使用前重复核对当前安装/schema，变动拒绝派发，不把该摘要永久视为能力证明。

本轮仍不提供逐请求 Token 账本、精确 Token/费用硬上限、多任务共享 Token 预留结算或全局外部写者锁。v2 共享准入、typed、固定只读、质量流程及其他真实后端切换拒绝；这些属于 R4 未支持范围。只核对核心可见的活动任务/检查和原生最新轮次，外部并发操作仍有竞态，不能宣称分布式 exactly once。现有会话累计观察保持未知增量，不把压缩成本算成零。

[R5 待确认清单](R5-EXPERIMENT.md)已准备单对实验：4 个工作轮次和 1 次压缩，固定任务/验收，记录失败与缓存条件。尚缺用户选定的模型、effort、账户/profile 与明确次数预算授权；真实模型、真实权限/profile 一致性、压缩质量及 Token 净收益均未测。没有 push、合并、发布或部署。

## R4 本轮验证记录

第一次完整隔离验证 `ASTERUN_VERIFY_TMP=/tmp/asterun-context-r4-full bash scripts/verify-offline.sh` 为 2512 通过、4 跳过。补齐排队证据复核、常驻入口限制及故障窗口后，在同一 clean-env venv 完整复测为 2523 通过、4 跳过，耗时 276.29 秒；日志 /tmp/asterun-context-r4-full-final.log，JUnit /tmp/asterun-context-r4-full-final.xml。跳过项均为真实后端，Antigravity 三份固定源码核验通过。

全量收集后又补了源原生 thread_id/backend_session_id 必须完整且一致的拒绝条件和两项回归。最终源码与独立 wheel 均运行上下文切换、原生生命周期、交接、输入载荷、累计用量和紧凑观察八个文件，分别 141 项通过。新增 R4 测试共 46 项。日志 /tmp/asterun-context-r4-targeted-final.log、/tmp/asterun-context-r4-wheel-final.log。wheel 测试复制到临时目录，清空环境、不附带 src/pythonpath，模块实际来自独立 venv 的 site-packages。

故障覆盖包括压缩 ACK-only、错误线程/旧轮次、缺失产物或持久历史、断连、失败、取消、压缩前/ACK 后/完成后的持久失败；新线程创建前、绑定后、提示发出前和受理后的故障，再启动只对账不重发。另覆盖意图写入前后中断、双 SQLite 连接竞争后继、重复键、已占用次数保留、原审批保留、源码/事件/策略/报告漂移、权限撤销、同工作区未决本地检查、同步入口和源线程引用冲突。旧验收不升级到新运行。测试先后暴露过外部绑定字段名写错、把被关闭的协议进程当作已完成和 fixture 的审批关键词误命中；修正夹具/断言后复测，没有把未决运行放宽为成功。

发行检查通过，固定 wheel/sdist 内运行文件逐字节匹配当前源码，文档链接和禁入内容检查通过。最终 wheel SHA256 为 414d8086636a891c8c519ff3c8c88b82106ab50a2f934cfc8d3276cf9fb6435e，sdist 为 0e0d7e2832d69e695dd8c009d25b0d05686ed9206458e32213e5f26837ee4a63；位置 /tmp/asterun-context-r4-dist-final，构建日志 /tmp/asterun-context-r4-build-final.log。测试、状态报告和 R5 实验材料不在发行包中。

R5 固定起点的九项 unittest 已离线执行：1 通过、6 失败、2 错误，符合故意保留缺陷的预期，不计入产品测试通过数。记录 /tmp/asterun-context-r4-r5-fixture/baseline.log；任务、验收和提示的 SHA 已固定。没有通过“把基线修好”或挑选成功样本来预设优化收益。

最终安装包另经 scripts/verify-installed-control.py 验证：fake 的 CLI/MCP 两任务成功，94 条 schema 记录通过，重启幂等重放和备份恢复通过，SQLite schema 仍为 6。记录 /tmp/asterun-context-r4-control-release.json。原生模型、账户和真实客户端交付字段仍是未验证，不能用本项代替 R5。

## 2026-09-24 R5 单对真实实验完成

用户确认沿用原生当前模型，并在首轮审批中止后追加一个工作轮次及主控逐项审批授权。实际使用 codex-cli 0.155.0-alpha.16.4、ChatGPT Pro、gpt-6-astra/high，累计 5 个工作轮次（含一个已取消轮次）和 1 次压缩。普通续接和压缩后续接均在原 Task 内完成，独立九项消费者测试全部通过，最终代码 SHA 一致；验收回写为 external_reported/passed，未决审批、队列和在途均为空。真实历史回读确认压缩轮次 completed 且有 contextCompaction，再接 completed 工作轮次。

普通续接的末次会话累计输入 138,462 Token，压缩组 158,908，多约 14.8%；缓存输入分别 130,432 和 121,856。短任务未显示输入节省，保留默认关闭。失败会话 10,871 总 Token 也纳入已知观察，三个独立会话已知总计 310,264，不含未完整观测的主控开销，不代表账单或周额度。工具循环与缓存条件有差异，不能把全部差值归因于压缩。完整口径、授权、故障和未验证项见 [R5 结果](R5-RESULTS-2026-09-24.md)。

本轮无需修改核心运行代码。私有证据和 SQLite 一致备份保存在本工作树 .local/token-optimization-r5-20260924，SHA 清单与数据库完整性均已核对；未复制凭据或原始推理。没有继续调用模型，也没有 push、合并或部署。

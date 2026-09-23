# Asterun 当前状态

[中文](STATUS.md) | [English](STATUS.en.md)

当前版本为 `0.1.0a13`，采用 Apache-2.0。首版功能范围已冻结，维护集中在现有缺陷、兼容性、现场验收和发行内容。使用入口见 [README](README.md)、[用户手册](docs/user-manual.md)和[发行说明](docs/release-v0.1.md)。

核心包含 CLI/MCP、常驻执行、任务与原生会话引用、权限和审批、幂等、取消与对账、调度、备份恢复及可选代码质量流程。配置 v1/v2、SQLite schema 6、runner-control/v1 和 capability profile 保持兼容。各 Agent 按配置提供能力，由调用方组织任务流程。

[外部主控工作流](docs/caller-orchestration.md)已实现可选紧凑状态、跳过事件的观察、按任务/工作区/会话汇总的只读原生用量报告，以及 `workflow-snapshot`、固定外部报告和同任务有限修复。默认观察与既有接口保持兼容；观察截止不取消任务，跳过事件不推进游标。本轮真实模型执行、原生客户端与净节省率仍待独立验证，用量报告不代表实付账单。

外部验收绑定当前运行、配置 revision、原任务输入、所选文件版本及报告字节；固定验收中的文件检查也必须属于 `target_paths`。外部报告保留 `external_reported` 来源，不升级为独立审查证据；`external_require_review` 持久保留已提出的审查要求，后续 evaluate 或 repair 不能将其降级。修复累计保留在原任务及既有上限内，不宣称 Grok 原生会话已续接。以下发布快照与现场记录保留原验证范围。

追加修复为用量明细分页设置条数和字节上限，同时保留完整查询总计；Claude 费用区分明确零值与未知值；紧凑查询限制原生字段大小；服务端提供有界紧凑事件页并保留游标；内部质量流程接管时清除当前外部验收绑定，保留审查要求及原运行中的历史报告。

2026-09-23 增加可选 Grok 原生日志同步：`asterun-grok sync-sessions` 支持历史补齐，`session_sync_home` 在文本和编码运行结束后自动导出两个原生日志文件，默认关闭。目标冲突、外部修改、符号链接和不完整记录不会覆盖；失败不改变任务结果。Nowdex 1.0.3 现场已导入 135 个会话中的全部 133 个有原生用量的会话，输入、缓存、输出、推理字段逐会话一致，总计 357,937,457 Token；这只是已有日志回填，不代表新增模型执行或实付账单。自动运行入口以隔离协议替身验证，真实模型自动同步仍待后续任务验证。

本轮完整隔离回归为 2298 通过、4 跳过；核心 wheel/sdist 内容及运行字节检查通过，安装后的自动同步协议替身、CLI/MCP fake 两任务、94 条 schema 记录、重启重放和备份恢复验证通过。默认实例部署运行提交 `f6a3564abb3d163d81e9d0fead3f3c48a2c2a848`，配置 revision 6 已显式启用 Grok `session_sync_home`；其它临时实例需分别配置。切换后 20 个任务、20 个运行、20 个会话的行内容与 1445 项审查产物保持一致。首次服务启动请求被 macOS 拒绝，确认未加载后恢复启动，最终 CLI/MCP 只读检查及运行配置回读通过；旧运行包、原始失败记录和状态备份均保留。没有新增生产任务或真实模型调用。

本轮维护于 2026-09-15 使用 Python 3.14.0 在隔离环境完成全套 pytest 回归：2249 通过、4 跳过，全套耗时 252.01 秒。Antigravity 的 3 份固定来源文件校验及 34 项发行包检查通过。该隔离验证包含真实离线协议进程、SQLite 重开与 CLI/MCP 入口，未调用真实模型或切换运行实例。固定 wheel 安装后的独立 fake 验收另行通过：CLI/MCP 两任务成功，94 条 schema 记录有效，重启重放与备份恢复通过；这些结果不代替真实后端验收。

2026-09-15，本机 `default` 实例已部署运行提交 `1fdd830cb00d6f2a4a4bf3e69cada92fa7677e11`，wheel SHA256 为 `2e01439f7067744b76c2faff164885bbb42013e38b1a008a6a59eaea8efe5c3f`。配置 revision 5、SQLite schema 6 保持不变；切换前后的 20 个任务、20 个运行、20 个会话、1301 条事件和 1445 条 review-artifact 记录一致。生产只读验收通过 CLI 紧凑查询、无事件观察、`task-events --compact`、用量报告和含缺失文件 manifest 的快照；用量报告遍历 9 页，总计保持一致且无重复条目。MCP 的 47 项工具发现、用量分页 schema 与读取、紧凑查询及紧凑事件页实测通过。本轮未新增生产任务或调用真实模型；旧安装与切换前备份保留，回滚检查通过。

公开源码首版以 `c19535e` 为快照来源，102 个运行文件与 `37bc705` 保持一致，保留全部 102 个测试文件。Python 3.11.16 完整离线回归为 2002 通过、4 跳过；本轮使用临时状态与离线后端夹具。核心与三个插件共 4 个包、8 份 wheel/sdist 已通过 34 项包装检查，4 份 sdist 重建和 4 个 wheel 离线安装均已验收。既有现场验证范围见发行说明，内容摘要与核验方式见[来源说明](docs/source-provenance.md)。

开发先核对当前提交、工作区和相关任务，默认离线入口为 `scripts/verify-offline.sh`。设计与维护索引见[设计](docs/design.md)、[开发规划](docs/development-plan.md)和[协议实现表](docs/protocol/IMPLEMENTATION.md)。

可选的 [GrokBot 模板](integrations/grokbot/README.md)已发布 `0.1.0-rc1`，并继续现场验收，使用独立 SQLite 账本和宿主 routine 跟踪普通任务。56 项模板离线测试与 34 项核心包装检查通过，并发提交专项重复 10 次全部通过；核心运行代码与已有协议保持不变。当前账户已验证云端安装及 fake 任务在离开聊天页面后的唤醒、原聊天交付、ack 和停止跟进。本机 Grok Build 已在真实项目目录完成短文任务，摘要核对通过；宿主回执记录 host_reported_delivered，末次 poll 无需跟进。发布后已在原生 UI 确认本机结果和回执吻合，三个临时 routine 全部暂停。固定 ZIP 已通过匿名下载与 manifest 核对，包内记录保留发布时状态，当前文档补记后续现场确认。[原生 Asterun 模板](https://x.ai/bot/4r3ysO1eA-6ZLI6cRoTNU)第 3 版已发布，11 条完整 memory、3 个完整技能、发布卡及公开页面分别核对通过。同账户导入尚未执行，等待用户在点击 Add to Grok Bot 时确认[分享条款](https://x.ai/legal/bot-sharing-terms)；云端真实模型、应用重启和独立新用户验收保留待办。分层结果见[模板验收记录](integrations/grokbot/acceptance.md)。

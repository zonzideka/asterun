# Asterun 当前状态

[中文](STATUS.md) | [English](STATUS.en.md)

当前版本为 `0.1.0a13`，采用 Apache-2.0。首版功能范围已冻结，维护集中在现有缺陷、兼容性、现场验收和发行内容。使用入口见 [README](README.md)、[用户手册](docs/user-manual.md)和[发行说明](docs/release-v0.1.md)。

核心包含 CLI/MCP、常驻执行、任务与原生会话引用、权限和审批、幂等、取消与对账、调度、备份恢复及可选代码质量流程。配置 v1/v2、SQLite schema 6、runner-control/v1 和 capability profile 保持兼容。各 Agent 按配置提供能力，由调用方组织任务流程。

当前工作区的[外部主控维护候选](docs/caller-orchestration.md)增加可选紧凑状态、跳过事件的观察、按任务/工作区/会话汇总的只读原生用量报告，以及 `workflow-snapshot`、固定外部报告和同任务有限修复。默认观察与既有接口保持兼容；观察截止不取消任务，跳过事件不推进游标。候选尚未部署到运行实例；真实后端、原生客户端与节省率仍待独立验证，用量报告不代表实付账单。

外部验收绑定当前运行、配置 revision、原任务输入、所选文件版本及报告字节；固定验收中的文件检查也必须属于 `target_paths`。外部报告保留 `external_reported` 来源，不升级为独立审查证据；`external_require_review` 持久保留已提出的审查要求，后续 evaluate 或 repair 不能将其降级。修复累计保留在原任务及既有上限内，不宣称 Grok 原生会话已续接。以下发布快照与现场记录保留原验证范围。

本维护候选于 2026-09-15 使用 Python 3.14.0 完成 `scripts/verify-offline.sh` 隔离验证：2205 通过、4 跳过。Antigravity 的 3 份固定来源文件校验及 34 项发行包检查通过。验证包含真实离线协议进程、SQLite 重开与 CLI/MCP 入口，未调用真实模型或切换运行实例。

公开源码首版以 `c19535e` 为快照来源，102 个运行文件与 `37bc705` 保持一致，保留全部 102 个测试文件。Python 3.11.16 完整离线回归为 2002 通过、4 跳过；本轮使用临时状态与离线后端夹具。核心与三个插件共 4 个包、8 份 wheel/sdist 已通过 34 项包装检查，4 份 sdist 重建和 4 个 wheel 离线安装均已验收。既有现场验证范围见发行说明，内容摘要与核验方式见[来源说明](docs/source-provenance.md)。

开发先核对当前提交、工作区和相关任务，默认离线入口为 `scripts/verify-offline.sh`。设计与维护索引见[设计](docs/design.md)、[开发规划](docs/development-plan.md)和[协议实现表](docs/protocol/IMPLEMENTATION.md)。

可选的 [GrokBot 模板](integrations/grokbot/README.md)已发布 `0.1.0-rc1`，并继续现场验收，使用独立 SQLite 账本和宿主 routine 跟踪普通任务。56 项模板离线测试与 34 项核心包装检查通过，并发提交专项重复 10 次全部通过；核心运行代码与已有协议保持不变。当前账户已验证云端安装及 fake 任务在离开聊天页面后的唤醒、原聊天交付、ack 和停止跟进。本机 Grok Build 已在真实项目目录完成短文任务，摘要核对通过；宿主回执记录 host_reported_delivered，末次 poll 无需跟进。发布后已在原生 UI 确认本机结果和回执吻合，三个临时 routine 全部暂停。固定 ZIP 已通过匿名下载与 manifest 核对，包内记录保留发布时状态，当前文档补记后续现场确认。[原生 Asterun 模板](https://x.ai/bot/4r3ysO1eA-6ZLI6cRoTNU)第 3 版已发布，11 条完整 memory、3 个完整技能、发布卡及公开页面分别核对通过。同账户导入尚未执行，等待用户在点击 Add to Grok Bot 时确认[分享条款](https://x.ai/legal/bot-sharing-terms)；云端真实模型、应用重启和独立新用户验收保留待办。分层结果见[模板验收记录](integrations/grokbot/acceptance.md)。

# Asterun 当前状态

[中文](STATUS.md) | [English](STATUS.en.md)

当前版本为 `0.1.0a13`，采用 Apache-2.0。首版功能范围已冻结，维护集中在现有缺陷、兼容性、现场验收和发行内容。使用入口见 [README](README.md)、[用户手册](docs/user-manual.md)和[发行说明](docs/release-v0.1.md)。

核心包含 CLI/MCP、常驻执行、任务与原生会话引用、权限和审批、幂等、取消与对账、调度、备份恢复及可选代码质量流程。配置 v1/v2、SQLite schema 6、runner-control/v1 和 capability profile 保持兼容。各 Agent 按配置提供能力，由调用方组织任务流程。

公开源码首版以 `c19535e` 为快照来源，102 个运行文件与 `37bc705` 保持一致，保留全部 102 个测试文件。Python 3.11.16 完整离线回归为 2002 通过、4 跳过；本轮使用临时状态与离线后端夹具。核心与三个插件共 4 个包、8 份 wheel/sdist 已通过 34 项包装检查，4 份 sdist 重建和 4 个 wheel 离线安装均已验收。既有现场验证范围见发行说明，内容摘要与核验方式见[来源说明](docs/source-provenance.md)。

开发先核对当前提交、工作区和相关任务，默认离线入口为 `scripts/verify-offline.sh`。设计与维护索引见[设计](docs/design.md)、[开发规划](docs/development-plan.md)和[协议实现表](docs/protocol/IMPLEMENTATION.md)。

可选的 [GrokBot 模板](integrations/grokbot/README.md)进入 `0.1.0-rc1` 验收，使用独立 SQLite 账本和宿主 routine 跟踪普通任务。56 项模板离线测试与 34 项核心包装检查通过，并发提交专项重复 10 次全部通过；核心运行代码与已有协议保持不变。当前账户已验证云端安装及 fake 任务在离开聊天页面后的唤醒、原聊天交付、ack 和停止跟进。本机真实后端前置检查因符号链接入口失败，尚未创建模型任务。真实后端、应用重启、原生模板分享和独立新用户验收保留待办。分层结果见[模板验收记录](integrations/grokbot/acceptance.md)。

# Asterun 项目指令

所有回答和报告使用中文，以简洁自然段落为主。

本目录是独立 Git 项目。先读 README.md、STATUS.md 和 docs/design.md；云端开发同时读取 docs/cloud-handoff.md。按 docs/development-plan.md 推进维护，调优参照 docs/tuning-plan.md。开始改文件前核对 git status --short、当前提交及可访问的相关任务。

Asterun 是通用 Agent 执行底座。核心通过 CLI/MCP 与调用方集成，GrokBot 为可选入口。宿主专有的账户、实例、模板或事件字段由入口适配层处理。

本仓包含当前核心、插件、测试和中英文文档，早期迁移材料保存在维护者的独立归档中。源码来源与公开文件范围见 docs/source-provenance.md。Antigravity 插件通过固定内容摘要和机械变换核对当前源码，运行 python3 packages/asterun-plugin-antigravity/scripts/verify-source.py 验证。

首版功能范围已冻结。按任务修复现有缺陷，保留协议、原生会话、权限、恢复和质量语义。默认验证使用临时目录与 fake 后端，具体测试与结果写入提交说明；真实后端、原生客户端和消息交付分别记录现场结果。

保持 STATUS.md 中的支持情况和未验证项准确。涉及其他项目时，先说明接口变化；部署使用独立的实例切换流程，保留已有任务与原生会话。发布包与凭据、会话和运行记录分开保存。

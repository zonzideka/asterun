# 云端开发

[中文](cloud-handoff.md) | [English](en/cloud-handoff.md)

检出 [Asterun 仓库](https://github.com/zonzideka/asterun)后，先核对当前提交和工作区改动，再阅读 [AGENTS.md](../AGENTS.md)、[STATUS.md](../STATUS.md)及[设计基线](design.md)。当前接口以 `src/asterun/` 和[协议实现表](protocol/IMPLEMENTATION.md)为准。

```sh
git status --short --branch
git log -3 --oneline
python3 packages/asterun-plugin-antigravity/scripts/verify-source.py
scripts/verify-offline.sh
```

离线检查使用临时 HOME、状态目录和 fake 后端。Python 最低版本为 3.11，依赖按 `pyproject.toml` 与 `requirements-lock.txt` 安装。需要单独复现测试时，参考[安装说明](install.md)。

首版功能范围已冻结，按任务修复现有行为并更新相关文档。Asterun 核心和可选插件均在本仓库，调用方通过 CLI/MCP 集成。调整接口时，在交接中写清调用参数、返回结果和兼容处理。

当前源码来源和公开范围见[来源说明](source-provenance.md)。Antigravity 插件在本仓核对固定文件摘要和机械变换，默认测试使用当前实现。

每项提交写明具体变更、实际运行的测试和结果。涉及配置或状态迁移时，补充恢复步骤；后端账户、原生客户端和消息交付按各自现场结果记录。部署另走实例切换流程，保留已有任务与原生会话。

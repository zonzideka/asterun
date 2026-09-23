# Grok 直调复盘首批实现验证

日期：2026-09-24。基线 `84ec1655f187a4a5fff65222aa43df8f0edd150d`，实现分支 `codex/grok-native-workflow-0924`。范围为 A00—A03 的首批能力，SQLite schema 6、runner-control/v1、旧配置默认行为和既有质量语义保持兼容。本次未调用真实模型、变更生产配置或部署。

## 实现与检查

`session_policy=resume` 只对编码模式生效。离线 CLI 替身覆盖同原生会话第二轮、确定取消和回合上限后续接、SQLite 重开、丢失/截断/外部追加日志、身份/凭据代际/profile/模型/工作区/CLI 变化、能力缺失和会话锁冲突。失败不静默新建会话。`session-resume --native` 只核对可续接条件，不冒充发送模型轮次。

Grok 错误测试覆盖 HTTP 402/429/401/403 的标准错误事件、stderr 和多行非 JSON 错误尾部。部分编辑后失败保留文件和 `REMOTE_STATE_UNKNOWN`，只增加白名单分类，原始诊断不进入持久结果。额度门禁复用持久运行记录，覆盖 v1 同 profile、v2 共享池窗口、重启和排队零新增派发；未决运行不被结清。

受控检查通过 CLI/MCP 输入固定检查名，验证权限、目标漂移、固定命令、输入副本、有限输出、超时及进程组清理。macOS 实际 sandbox-exec 测试确认网络、原工作区读写和继承的秘密环境变量被阻止。加载错误、全部跳过和空测试集不能仅凭退出码 0 通过。诊断经版本绑定报告进入同任务修复，重放不增加运行；报告不能替代独立审查。

## 执行结果

| 检查 | 结果 |
|---|---|
| `scripts/verify-offline.sh`，Python 3.14.0，清洁环境与临时 HOME | 共 2375 项，2371 通过，4 个真实后端测试按开关跳过 |
| Antigravity `verify-source.py` | 3 份固定来源文件摘要/机械变换核对通过 |
| `scripts/check-release-package.py` | wheel/sdist 成员、许可、正文链接与运行字节核对通过 |
| `examples/caller-workflow/demo.py` | 真实 CLI、fake 后端：一个任务两次运行、外部失败报告、幂等修复、复验与目标漂移失效通过 |
| 独立 wheel 的续接、错误/额度、局部检查、权限和配置回归 | 231 项通过；其中一项首次缺少测试夹具构建依赖，补齐 setuptools/wheel 后单独复测通过 |
| `scripts/verify-installed-control.py --backend fake` | CLI/MCP 各一任务、94 条 schema 记录、幂等重放、重启及备份恢复通过 |

首轮非清洁全量回归暴露了同步派发向旧后端替身传递新 `native` 参数的兼容错误。修复为后端显式声明支持后才传递；相关测试及上述清洁全量回归均通过。没有通过修改旧权限断言来掩盖失败。

验证 wheel 的 SHA-256 为 `41ff7aa2054767503e3c4f24736e72c542a43b0209918ddcbb570d4f3ba53508`。本机原始日志和安装报告保存在 `/tmp/asterun-native-0924.CBMl91/`：`offline.log`、`package-check.log`、`installed-features.log`、`installed-plugin-recheck.log` 和 `installed-control.json`。该目录是临时验证产物，不是生产运行实例或长期备份。

## 尚未覆盖

真实 Grok 模型续接、原生取消/客户端显示、Linux OS 沙箱现场和 G10/X01/X02 完整项目回归未执行。局部检查沿用 64 文件/256 KiB 快照，最多同步等待 60 秒；自动诊断缓存和异步检查调度尚未实现。A04—A08 保持后续批次。认证文件刷新可能触发保守绑定拒绝，后续稳定的原生账户身份接口需单独验证；当前不会读取或导出凭据正文。以上结果不构成模型质量、订阅实付费用或 Token 净节省率证据。

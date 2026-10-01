# 标准 skill 入口

Asterun 通用技能位于 `integrations/skills/asterun/`，可以复制或独立打包到支持 Agent Skills 的客户端。入口由 `SKILL.md`、按需说明和 Python 标准库脚本组成；执行状态、权限、会话、幂等与恢复仍由既有核心负责。它与 GrokBot 模板的聊天交付技能分开使用。

先安装 Asterun 核心并配置常驻实例，接入方式见[安装说明](install.md)。将技能目录放到客户端的技能发现目录，例如 Codex 的项目 `.agents/skills/asterun` 或用户技能目录；不要覆盖同名自定义技能。复制技能不会安装核心、登录账户或停用 MCP。验证可以显式调用 `$asterun`；自动匹配和其它客户端的运行效果需在对应客户端独立确认。

使用时提供已安装 CLI、已有实例状态目录以及核心状态目录之外的私有证据目录。技能脚本的 `--help` 列出入口；基本流程为 `discover`、`submit`、`wait`，确定终态后按任务需要 `snapshot`、`evaluate` 和 `repair`。完整参数与异常处理见[技能工作流说明](../integrations/skills/asterun/references/workflow.md)。

脚本通过 `--connect` 执行一次既有 CLI 操作，不直接打开 SQLite。等待在进程内使用紧凑状态和跳过事件的退避观察，stdout 返回状态、引用和证据路径。响应、请求和 stderr 留在独立私有目录；评价和修复请求由快照绑定机械生成。脚本不自动批准审批、追加预算、重派未知结果或发起上下文压缩。

运行成功、验收、独立审查、终止和发布分别呈现。原目标、所选文件、配置、真实报告摘要、幂等与修复上限由核心重新核对。观察到期退出码 124，后台继续运行；客户端超时或响应损坏保留原意图，结果未知时按原任务对账。外部报告仍是 external_reported，不能替代独立审查。

维护者提交技能源码后运行 `python3 scripts/package-standard-skill.py --output /path/to/asterun-skill.zip` 构建固定文件清单的独立包。ZIP 中的 `asterun/manifest.json` 包含源码提交与每个成员的 SHA-256；核心源码包也包含技能源文件和打包脚本。核心 wheel 不隐式安装用户技能或改变客户端配置。

有界返回字节与模型往返流程可用 fake 测量，主控 Token、模型质量、缓存与实际订阅额度需另做同条件真实对照。增加技能不会自动移除已有 MCP 工具描述；是否减少它们由宿主工具加载方式及入口配置决定。验证与当前限制见仓库 STATUS 和本轮改造计划。

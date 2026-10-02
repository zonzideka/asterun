# 标准 skill 入口

Asterun 通用技能位于 `integrations/skills/asterun/`，可以复制或独立打包到支持 Agent Skills 的客户端。入口由 `SKILL.md`、按需说明和 Python 标准库脚本组成；执行状态、权限、会话、幂等与恢复仍由既有核心负责。它与 GrokBot 模板的聊天交付技能分开使用。

标准发行版最低需要 Asterun CLI 和常驻核心 `0.1.0a14`；接入见[安装说明](install.md)。公开标签版 `0.1.0a13` 没有本技能使用的紧凑等待与快照接口。历史主线构建仍可能报告 a13，脚本仅对该版本补充 CLI 参数与常驻核心只读接口探测，通过后兼容，不提供跳过门禁的开关。每次操作核对当前应答核心，避免提交成功后才发现无法等待；连接、超时、未知版本与确定的不兼容分别报告。

从 [a14 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a14)取得核心 wheel、`asterun-skill-0.1.0a14.zip` 和 `SHA256SUMS`，先核对下载摘要，再安装到对应位置；也可从固定源码提交构建。核心版本、源码 SHA 与 skill 清单应一起保存。GrokBot 模板仍按 `release-lock.json` 安装公开 a13，该实例不能使用本技能。模板锁和标准技能分开验证和发布。

将技能目录放到客户端的技能发现目录，例如 Codex 的项目 `.agents/skills/asterun` 或用户技能目录；不要覆盖同名自定义技能。复制技能不会安装核心、登录账户或停用 MCP。验证可以显式调用 `$asterun`；自动匹配和其它客户端的运行效果需在对应客户端独立确认。

使用时提供已安装 CLI、已有实例状态目录以及核心状态目录之外的私有证据目录。技能脚本的 `--help` 列出入口；基本流程为 `discover`、`resources`、`submit`、`wait`，确定终态后按任务需要 `snapshot`、`evaluate` 和 `repair`。完整参数与异常处理见[技能工作流说明](../integrations/skills/asterun/references/workflow.md)。

脚本完成兼容探测后通过 `--connect` 执行一次既有 CLI 业务操作，不直接打开 SQLite。等待在进程内使用紧凑状态和跳过事件的退避观察，stdout 返回状态、引用和证据路径。响应、请求和 stderr 留在独立私有目录；评价和修复请求由快照绑定机械生成。脚本不自动批准审批、追加预算、重派未知结果或发起上下文压缩。

`discover` 区分模拟后端和真实后端并保留执行模式；`resources` 返回当前授权的工作区名称与路径。仅有 fake 时可以验证协议与验收流程，不能声称已生成用户所需内容。

运行成功、验收、独立审查、终止和发布分别呈现。原目标、所选文件、配置、真实报告摘要、幂等与修复上限由核心重新核对。观察到期退出码 124，后台继续运行；客户端超时或响应损坏保留原意图，结果未知时按原任务对账。外部报告仍是 external_reported，不能替代独立审查。

维护者提交技能源码后运行 `python3 scripts/package-standard-skill.py --output /path/to/asterun-skill.zip` 构建固定文件清单的独立包。ZIP 中的 `asterun/manifest.json` 包含源码提交、`minimum_core_version` 与每个成员的 SHA-256；核心源码包也包含技能源文件和打包脚本。核心 wheel 不隐式安装用户技能或改变客户端配置。GrokBot 模板 `0.1.0-rc1` 的发布锁保持不变。

有界返回字节与模型往返流程可用 fake 测量，主控 Token、模型质量、缓存与实际订阅额度需另做同条件真实对照。增加技能不会自动移除已有 MCP 工具描述；是否减少它们由宿主工具加载方式及入口配置决定。验证与当前限制见仓库 STATUS 和本轮改造计划。

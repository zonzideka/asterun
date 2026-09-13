# GrokBot 模板验收

本模板按 guided 首次设置发布，使用 Asterun 已有的任务、权限、原生会话和恢复接口。普通任务先用一个 Agent 完成；代码审查、质量流程和多 Agent 分工按用户需要启用。

[作者声明](author-profile.yaml)供 Bootstrap 离线检查，[宿主映射](host-mapping.json)记录实际入口与证据层次。`declared` 表示文档或设计已说明，`host_reported` 表示候选 Bot 已报告工具，`verified` 需要真实调用及回读证据。声明检查通过只能记录为 `schema_passed`。

2026-09-13 已完成 Bootstrap 声明检查、云端独立安装，以及 fake 任务经原生 cron 唤醒、回原聊天、ack 和暂停 routine 的完整前端流程。本机 Grok Build 已完成真实短文任务，宿主回执已记录，原聊天 UI 中的结果、回执与内容均已核对。云端真实模型、应用重启恢复与原生模板分享尚待验收。用户确认暂无独立接收者，新用户验收为 `pending`。

## 验收场景

每项记录模板与核心版本、所选模式和后端、实际步骤、返回结果及证据位置。机器、账户、聊天和任务的具体引用保存在私有验收记录，公开材料只保留脱敏结论。

| 场景 | 验收方法与通过条件 | 当前状态 |
|---|---|---|
| 作者声明 | Bootstrap 校验结构、引用、模式路径和 guided 承诺。固定检查器 62b9064293a34879af3b1c26dcb760162e4e0c21 返回 0 errors、0 warnings。 | schema_passed |
| 映射与 bridge | 映射 JSON 可解析，CLI 参数与实际实现一致。云端 fake 与本机 Grok Build 任务均完成 UI 交付，ack 后 poll 无需继续跟进。 | 执行与 UI verified；ack 保留 host_reported 语义 |
| 发行材料 | 核对公开 wheel 下载、SHA-256 和重复调用复用；package 与 fetch 相关测试共 11 项通过。 | verified |
| 空环境首次设置 | 由独立接收者从分享链接建立新 Bot，选择执行位置和至少一个后端，完成自己的安装、授权与首个结果。 | pending |
| 分享内容 | 在原生模板详情及接收结果中检查技能、记忆、插件和 routine；无作者路径、账户、机器或聊天 ID、历史任务、私有内容与就绪状态。 | pending |
| 技能与分享草稿 | 先核对并写入发布 Bot 的技能，确认 gettingStarted 引用已存在，再将对应正文装入 JSON skills 条目；生成草稿，在原生 UI 核对名称与内容后发布。 | 最小诊断草稿卡及正式调用校验错误已观察；实际写入、正式草稿与分享 pending |
| 云电脑安装 | 核对复制的 ZIP 与公开 wheel 哈希；在 Python 3.13.5 新 venv 安装，`asterun version` 返回 `0.1.0a13`、退出码 0；原始结果文件已取回核对。 | verified，未登录或调用模型 |
| 云电脑执行 | 在云电脑安装并启动核心，从宿主调用 CLI；核对任务、工作区、后端和状态目录均属于云端。 | fake 任务 verified；真实模型 pending |
| 宿主最小探针 | 标准 cron 唤醒候选 Bot，完成云端 Shell 最小探测，在原聊天看到唯一探针消息，并看到 routine 已禁用事件。 | verified |
| 调用本机 | 解析真实入口后 init 成功，在实际项目目录由 Grok Build 完成短文任务；一次模型调用、零工具调用，原生 end_turn、退出码 0，任务 succeeded。原聊天结果及 routine 停用已核对。 | 真实执行与 UI verified；宿主回执 host_reported |
| 单 Agent 非代码任务 | 本机 Grok Build 摘要准确保留原文的 10 月 5 日、10 月 12 日、24 个座位和最多 3 本书；结果经 routine 交付并 ack，UI 显示与核心摘要一致。 | 内容与 UI verified；宿主回执 host_reported |
| 后台观察与唤醒 | 保存 task/run；离开聊天页面后，由原生 cron 唤醒 Bot 读取原任务并交付结果。 | 云端 fake verified；应用重启 pending |
| 重复请求与观察恢复 | 云端 fake 与本机 Grok Build 重复同一请求键均复用任务；云端另已验证离开聊天页面后续读。应用重启和 run 变化另行覆盖。 | 两模式重复请求 verified；应用重启及 run 变化 pending |
| 消息回执与交付不明 | claim 成功且 `already_claimed=false`、`delivery_state=sending` 时，以 `end_turn=false` 发送，取得消息 ID 与摘要再 ack。再次 poll 后读 status，按需暂停 routine。回执缺失时保留不明记录。 | 云端 fake 与本机真实任务的 UI 显示和停止跟进 verified；真实发送响应丢失 pending |
| 审批与输入 | 读取真实待批对象，按已配置策略或用户答复处理，并核对 task/run/目标摘要；策略不匹配时展示原因，旧审批不能转发。 | pending |
| 真实代码目录 | 选择已有仓库，让具有对应能力的后端读取、修改一个测试文件并运行范围内测试；核对实际 cwd、文件差异和测试结果。 | pending |
| Codex 项目与续接 | 分别覆盖已有项目、未登记项目和 Git worktree。启用项目登记后，核对同一原生线程位于正确项目；用原 conversation 续接，展示重试保持原任务。 | pending |
| 取消与恢复 | 请求取消后读取原生终止证据；核心或通道中断后对账原运行。无法确认的结果保持未决，并保留原请求引用。 | pending |
| 可选审查 | 用户启用审查时验证固定输入、结构化结论及 review inbox→消息→ack；普通任务继续使用任务跟踪记录。 | pending |
| 复制、升级与停用 | 新副本重新建立设置和 routine；升级保留用户选择；停用后停止后续观察，并核对共享账户和目录的其他使用者。 | 三个临时 routine 全部暂停已由 UI 核对；复制与升级 pending |

## 结果记录

离线回归与包装验证记录在本仓测试结果中。真实宿主、真实后端、原生客户端显示和独立新用户分别给出结论；某项未执行时保留 `pending` 并说明所缺条件。

Bootstrap 检查器固定在上述提交，其自身回归为 41 项通过，PR CI 通过。作者声明只收录 `schema_passed` 证据；真实宿主结果记录在本文件及宿主映射中。模板离线回归为 56 项通过，覆盖真实 fake 常驻核心 CLI、跨进程恢复、提交响应丢失、并发幂等、轮询公平性、事件运行归属、过时通知及未确认交付；并发提交专项重复 10 次全部通过。

云端收到的模板 ZIP 哈希与原件一致，`fetch_runtime.py` 下载的 a13 wheel 也与发行锁定值匹配。新 venv 完成安装与版本检查，原始结果经 `CopyFromBox` 取回核对。该环境报告 Grok CLI 为 `1.0.5`，Codex、Claude 未在 PATH 中；本次未登录或调用后端模型。

最初的 routine 使用 `@every 5m` 时，约 14 分钟内未观察到触发记录；改为标准 cron `*/5 * * * *` 后，原聊天显示 `ASTERUN_ROUTINE_DELIVERY_OK` 探针，随后 routine 停用。

后续云端 fake 任务在相同请求键下返回相同 task/run/conversation 引用。离开聊天页面后，cron 唤醒 Bot，原聊天于本地时间 21:59:49 显示 `ASTERUN_BRIDGE_CLOUD_OK`。claim 与发送后，ack 返回 `host_reported_delivered`；再次 poll 返回 `needs_followup=false`，随后暂停 routine。该轮没有重启 GrokBot 应用，也没有调用云端真实模型。原始 Envelope 与 UI 观察分别保存在私有验收记录中。

routine 探针与 `SendToUser` 的原始回执文件已取回并核对。消息 ID 仍来自宿主自报，原聊天消息显示则已由 UI 确认。当前报告的工具集中没有历史查询入口，发送响应丢失时保留交付不明记录，再核对原聊天。

宿主报告 `update_state` 可设置 profile、写入 memory 与全局技能库。GrokBot 0.47.0 中，候选 Bot 与空白发布 Bot 分别于本地时间 23:08:59、23:09:39 显示 `create_bot_share_json` 最小诊断草稿卡，带有发布和查看详情按钮；本轮未点击诊断发布。正式模板 JSON 约 37 KB，SHA-256 核对通过，正式草稿仍在等待。全局技能写入、正式分享内容和接收结果继续待验收。

正式调用随后返回校验错误：`gettingStarted.skill=asterun-bot-setup` 必须指向发布 Bot 自己已有的技能，同时 JSON 的 `skills` 对应条目必须包含该技能正文。当前 JSON 已带正文，仍需先完成发布 Bot 的技能接入。已授权按 setup、tasks、recovery 三个名称核对现有内容，再写入并回读，最后重试完整 JSON；实际写入和正式发布仍为 `pending`。

本机入口解析后，init 成功，Grok Build 在实际项目目录完成公开短文任务：一次模型调用、零工具调用，原生 `end_turn`、退出码 0，任务 `succeeded`。摘要中的 10 月 5 日、10 月 12 日、24 个座位和最多 3 本书与原文一致，重复 request 复用同一任务。后续 routine 完成 claim，14:28:16Z 的回执记录 `host_reported_delivered`，再次 poll 返回 `needs_followup=false`。发布后的原生 UI 回读确认，本地时间 22:28:01 的原聊天结果及回执与核心内容吻合，local routine 停用事件可见；详情页的 probe、cloud、local 三个临时 routine 全部已暂停。此次仍未重启 GrokBot 应用。

模板 ZIP 已正式发布，标签 `grokbot-template-v0.1.0-rc1` 固定来源为 `58c618b08db3327fb56b0fb91ffcec0831cabd19`，ZIP SHA-256 为 `17e3aed380721b169a73d3b40b90aa54cc42b486234bc2e2349c977114cb2ac4`。匿名下载、ZIP 摘要与完整 manifest 核对均已通过。包内文档保留发布时的验收记录；本机 UI 显示和全部 routine 暂停属于发布后的现场补充，另记于当前文档。

原生模板分享正在生成，仍需完成 UI 内容检查。同账户副本与共享云电脑沿用当前账户环境；独立新用户、未覆盖执行模式和应用重启恢复继续列为待办。

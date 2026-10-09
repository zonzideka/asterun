# Asterun 当前状态

[中文](STATUS.md) | [English](STATUS.en.md)

2026-10-09 内置 Antigravity 后端增加可选 `timeout_seconds`，范围 30 到 3600，省略时仍为 300 秒，命令行仍是 `5m`。该值同时用于 `--print-timeout` 和适配器等待。v1 写在 `backends.<name>`，v2 写在内置连接的 `options`。配置 schema 仍是 v1/v2，不改 SQLite。外部插件源码升到 `1.0.2`，只为同步 vendored `build_command`；worker 仍调用默认 5 分钟，自身期限仍是最多 20 秒。`1.0.2` 尚未随标签发布，已发布包仍是 a17 的 `1.0.1`，换装后需重新注册。原生 SUCCESS 但带工具错误、步骤错误或软拒绝时，仍记为 `ANTIGRAVITY_RUN_INCOMPLETE`，证据留在 `native` 计数字段和正文里。`v*` 标签的发行工作流已进入仓库，只用 `GITHUB_TOKEN` 与 OIDC，尚未对现有标签运行。没有新标签，核心版本仍是 `0.1.0a17`。

2026-10-09 把安全边界修改 rebase 到 `3ad783d8`。状态记录同时保留 #14 的 `slot_held` 与结果落盘段落。英文交接写上 runner 白名单、实际入口钉定，以及 `-m` 启动的 `-I -S -B`。插件 wheel 冒烟在钉定注册后经隔离启动调用 `plugin.describe`。核心仍是 `0.1.0a17`，插件源码仍是 `1.0.2`。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验和插件版本检查通过（五个副本文件，`source_git_object_checked` 为 true，版本 `1.0.1 -> 1.0.2`）。pytest 收集 2761 项，2724 通过、36 跳过、1 失败。失败项仍是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`，`read_scope.py` 与该测试未改。`python3 scripts/smoke-plugin-wheels.py` 通过：三个插件 wheel 安装后可以导入 worker，钉定注册，并用 `-I -S -B` 取得 `plugin.describe`。

2026-10-09 把外部插件 runner 收成两种白名单形态：绝对路径解释器加 `-m` 模块，或单独一个绝对路径可执行文件。`-c`、其他解释器选项、相对路径和「解释器加脚本」在注册时拒绝。入口只在钉定树内查找，注册不再启动解释器，因此不会执行 `site`、`.pth` 或 `sitecustomize`。`-m` 的实际启动使用 `-I -S -B`，只注入已钉定的导入根。插件副本版本只要带预发布标记就不能通过，即使它按 PEP 440 高于基线。核心仍是 `0.1.0a17`，插件源码仍是 `1.0.2`。已经用 `python /path/worker.py`、相对路径、`-c` 或其他解释器参数写成 runner 的配置无法加载，需要改成 `-m` 或单个绝对路径可执行文件后重新注册。仍使用 `-m` 的配置可以加载，但不再读取 `PYTHONPATH`，也不再执行 site 钩子；依赖 `.pth` 或 `sitecustomize` 找代码的安装需要把模块放进钉定树。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验和插件版本检查通过（五个副本文件，`source_git_object_checked` 为 true，版本 `1.0.1 -> 1.0.2`）。pytest 收集 2748 项，2711 通过、36 跳过、1 失败。失败项仍是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`，`read_scope.py` 与该测试未改。`python3 scripts/smoke-plugin-wheels.py` 通过。

2026-10-09 把外部插件运行摘要绑到实际入口。`runtime_path` 必须覆盖 runner 将要执行或导入的代码：`python -m package.worker` 钉在该解释器导入路径上的包目录或包含该包的安装树；直接脚本钉在脚本文件或其所在目录。只钉解释器、wheel 或无关副本会在注册时拒绝。执行前重新计算这份代码的摘要，摘要变化或入口不再落在其中时拒绝启动。插件副本版本检查改为 PEP 440 严格高于基线，降版本和预发布不能通过。核心版本仍是 `0.1.0a17`，插件源码仍是 `1.0.2`。已经把解释器或无关路径写成 `runtime_path` 的配置无法加载，需要按实际安装树重新注册。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验和插件版本检查通过（五个副本文件，`source_git_object_checked` 为 true，版本 `1.0.1 -> 1.0.2`）。pytest 收集 2745 项，进度标记为 2708 通过、36 跳过、1 失败。失败项仍是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`，`read_scope.py` 与该测试未改。`python3 scripts/smoke-plugin-wheels.py` 通过：三个插件 wheel 安装后可以导入 worker，并用包目录摘要完成注册和复核。

2026-10-09 收紧子进程环境、外部插件钉定和 Antigravity 来源核验。核心版本保持 `0.1.0a17`，协议、SQLite 和配置 schema 不变。Codex 与 Claude 子进程只接收白名单环境，保留 HOME、PATH、语言区域、代理和证书路径，以及 Codex 的 `CODEX_HOME`、Claude 的 `CLAUDE_CODE_OAUTH_TOKEN` 和 `CLAUDE_CONFIG_DIR`。`extra_env` 只追加变量名，不保存秘密值，并拒绝加载器变量。Claude 普通任务默认不加载用户 hooks 与 MCP；`load_user_settings: true` 只恢复普通任务，快照审查仍隔离。外部插件注册必须钉定 `runtime_path` 与 `runtime_sha256`。`allow_unpinned_runtime` 默认关闭，打开后也只能加载配置，不能执行。Antigravity 来源核验覆盖 `_vendor` 全部五个文件，失败时非零退出，来源提交改为本仓存在的 `9610e99`。插件源码为 `1.0.2`，尚未发布；a17 发行页的 wheel 仍是 `1.0.1`。

已部署实例升级核心后，Codex/Claude 不再继承未列入白名单的宿主变量；依赖这些变量时要把名字写入 `extra_env`。依赖用户 hooks 或项目 MCP 的 Claude 任务要显式打开 `load_user_settings`。没有运行摘要的外部插件配置无法加载。换装 `1.0.2` 后安装树摘要变化，须重新注册。macOS 服务的 SAFE_ENV 没有扩大，服务进程里本来没有的变量不会被 `extra_env` 找回。真实 Codex、Claude 登录和原生客户端尚未复测。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验和插件版本检查通过（五个副本文件，`source_git_object_checked` 为 true，版本 `1.0.1 -> 1.0.2`）。pytest 进度标记为 2709 通过、47 跳过、1 失败。失败项仍是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`：同长度覆写依赖 inode 时间变化，本机时间戳粒度内不更新，`read_scope.py` 与该测试未改。第一次全量还因英文安装文档的 ADR 相对链接不在 sdist 中失败，已改为 `../adr/0010-version-bound-quality-workflow.md`，随后单独复测 `tests/test_packaging.py::test_release_artifacts_exclude_references` 通过。`python3 scripts/smoke-plugin-wheels.py` 通过：三个插件 wheel 安装后可以导入 worker，并用运行树摘要完成注册和复核。

2026-10-09 把并发名额收成运行记录上的 `slot_held`，由调度器按 run id 幂等投影。审批结果写盘失败后，重试或重启即使只看到运行中的进度，也会写下审批结论，不再停在转发中。结果和保留副本都没写上时，随后成功的重试仍把已释放名额写入状态库，重启不会把名额占回去。同一条运行重放结果后再恢复，只占一个名额。终态去掉该标记，对外视图不包含它。没有该标记的旧记录仍按操作是否已派发、以及有没有释放标记来判断。SQLite schema 仍为 6，协议状态和错误码不变，版本号未改。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验通过；pytest 收集 2739 项，2702 通过、36 跳过、1 失败。失败项仍是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`：同长度覆写依赖 inode 时间变化，本机 tmpfs 在约 4ms 粒度内不更新 `st_mtime_ns`。`read_scope.py` 与该测试未改。此项未部署，也未用真实后端验收。

2026-10-09 修复核心结果落盘与重启对账。结果的多步写入放在同一个事务里；失败则整次回滚，运行进入 `pending_reconcile`、操作进入 `unknown`，释放本次并发名额。完整结果留在内存，并尽量写入运行记录；状态库暂时写不进去时，后续轮询按有上限的退避继续重试，直到落盘，不自动重派。工作线程因落盘失败中断后补发的通用待对账不会覆盖这份结果。前台同步派发、取消确认和审批回复的结果写入走同一事务。重启时，已标记释放名额或找不到操作记录的运行不再占回名额，也不重复执行。进程关闭时仍在执行、且没有释放标记的未知运行，重启后继续保留预算占位。终态检查点没写上时同样整次回滚；重启只重放已保留结果，检查点在写入成功后才算捕获，不重新调用后端。SQLite schema 仍为 6，协议状态和错误码不变，版本号未改。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验通过；pytest 收集 2735 项，2698 通过、36 跳过、1 失败。失败项仍是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`：同长度覆写依赖 inode 时间变化，本机 tmpfs 在约 4ms 粒度内不更新 `st_mtime_ns`。`read_scope.py` 与该测试未改。此项未部署，也未用真实后端验收。

2026-10-09 为 Codex 后端增加可选 `approval_policy`。省略时普通任务仍发送 `approvalPolicy=untrusted` 与 `sandbox=workspace-write`，并继续覆盖 `~/.codex/config.toml`。可改为 `on-request` 或 `on-failure`，在工作区沙箱内自动执行，越界或沙箱失败时仍询问。`never`、granular 和 `danger-full-access` 在配置校验时拒绝。固定读取仍为 `never` 与 `read-only`。配置 schema 仍是 v1/v2，不改 SQLite。此项尚未用真实 Codex 验收，随 `0.1.0a16` 发布。

2026-10-09 发布外部 Antigravity 插件 `1.0.1`：a16 已更新插件内 `_vendor/profile.py` 的 Linux x86_64 支持，但插件仍为 `1.0.0` 且未发新包。本次只升插件与 manifest 版本，并随 a17 发布插件 wheel 与源码包；插件代码与核心行为相对 a16 不变。

当前版本为 `0.1.0a17`，采用 Apache-2.0。首版功能范围已冻结，维护集中在现有缺陷、兼容性、现场验收和发行内容。使用入口见 [README](README.md)、[用户手册](docs/user-manual.md)和[发行说明](docs/release-v0.1.md)。本次预发布的核心与标准 skill 资产及校验文件见[对应版本发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a17)；下方按日期保留各批次当时的验证和部署事实。

2026-10-09 Antigravity profile 按平台核验 CLI 1.2.0 的 `webm_encoder`。macOS 仍只接受已核验的 12781442 字节摘要 `9cf13e875e7ffb9ef3fe1fc1a177c744bd8ee5a6651a2ec47cda307a009e8d11`。Linux x86_64 增加官方构建的 17056035 字节摘要 `45c1b1edd50159fbd4eac95ffd82df97a79b8c345fc43408ddae09230a304ed6`。Linux 的空 Playwright 缓存为 `~/.cache/ms-playwright-go/1.57.0`，macOS 仍只接受 `Library/Caches/ms-playwright-go/1.57.0`。未登记摘要、其他架构的编码器和额外二进制继续拒绝。内置技能清单未改。agy 1.3.2 因技能文件差异仍不在支持范围内；扩展方式是为该版本单独登记内置文件、各平台编码器摘要、缓存形状和 `upstream_version` 门禁。本项只改离线 profile 检查，没有调用真实 agy，也没有切换运行实例。Python 3.12.3 上 `scripts/verify-offline.sh` 的来源核验通过；pytest 收集 2659 项，2622 通过、36 跳过、1 失败。失败项是既有 `tests/test_read_scope.py::test_mutation_while_reading_rejected`：同长度覆写依赖 inode 时间变化，本机 ext4/tmpfs 在约 4ms 粒度内不更新 `st_mtime_ns`，`read_scope.py` 与该测试未改。main 的最近 GitHub CI（run 37126600286）为成功。跳过包含未开启的真实后端和缺少 bubblewrap 的 OS 沙箱用例。Antigravity profile 新增用例通过。

2026-10-03 后续排查发现真实 Grok 编码任务达到回合上限，绑定会话的 end 已返回且 CLI 以 1 退出，却被旧适配器记为未知，持续占用并发名额。修复明确未完成的终态判定，已有误判仅通过匹配任务/运行的持久化回执显式对账；超时、协议损坏和不匹配继续未决。标准 skill 增加 scheduler 与单任务 reconcile，队列摘要提供授权内的占位引用。专项 200 项通过，全套隔离回归 2644 项通过、4 项真实后端跳过；本次修复不在已发布 a14 标签内，修复构建 `57de00e` 的核心与技能已更新到本机，原未知运行经显式对账确认失败结束，占位释放，历史用量与无关记录保持不变；本机安装与恢复事实见[后续排查记录](docs/validation/skill-runtime-recovery-2026-10-03.md)。

2026-10-03 修复标准 skill 接入：在业务提交前区分公开 a13 与具备新接口的历史自编 a13，后者须通过 CLI 和应答核心的只读能力探测；新增授权工作区发现、执行模式提示、用量分页及大响应处理。实现提交 `d837dff` 的技能包已更新到本机，旧版可恢复，现有核心进程、入口与配置不变。五组合兼容矩阵、独立消费者 14 步及独立 wheel 两项端到端验收通过。完整离线回归 2610 通过、4 跳过、1 次版本探测超时；该失败场景随后连续三次复测通过，原整套命令未全绿。没有新增真实模型任务或公开发行，详情见[修复与安装记录](docs/validation/standard-skill-repair-2026-10-03.md)。

2026-10-02 标准 skill 与已发布 `0.1.0a13` wheel 不匹配：PR #5 的 `wait`/`snapshot` 依赖其后合入的 `task-watch --compact --no-events` 与 `workflow-snapshot`。源码版本改为 `0.1.0a14`，技能脚本在任何命令前用短超时探测 `asterun version` 和 `asterun diagnose`：过低返回 `CORE_VERSION_UNSUPPORTED`；探测失败分别返回 `CLIENT_TIMEOUT` / `CORE_UNREACHABLE` / `CORE_VERSION_UNKNOWN`，变更与只读命令都不发给未核验的核心。GrokBot 模板 `0.1.0-rc1` 仍锁定已发布 a13，在 a14 发布并更新锁之前不能与标准 skill 一起使用。维护者需打 `v0.1.0a14` 标签、发布 wheel 并在核对 SHA-256 后决定是否更新 GrokBot 锁。本批未打标签、未发布 wheel、未切换生产实例。

2026-10-01 推送验收修正了依赖门禁测试对宿主 OS 沙箱的隐含依赖：该逻辑测试使用合成检查结果，覆盖组合检查失败和沙箱不支持时均不能验收通过，不改核心隔离行为。本机依赖、阶段门禁、局部检查和后台检查专项 48 项通过；GitHub runner 缺少 bubblewrap 时，真实沙箱用例仍按现有规则跳过，不能据此宣称 Linux 现场隔离已验证。当前提交的 CI 与合并状态见 [PR #5](https://github.com/zonzideka/asterun/pull/5)。

2026-09-30 完成[标准 skill 入口](docs/standard-skill.md)改造：通用 SKILL.md、按需工作流说明和标准库脚本连接既有常驻核心，覆盖实际后端发现、一次提交、紧凑等待、证据读取、固定快照、真实报告评价、同任务有限修复、取消和用量。完整响应留在私有文件，摘要最多 12 KiB；绑定由快照机械生成并由核心复核，脚本不自动批准或重派未知结果。CLI/MCP、数据库及核心执行代码未变，GrokBot 交付独立保留。

本批清洁离线回归 2553 项通过、4 个真实后端跳过；独立 wheel 环境的技能专项 28 项通过，实际模块来自 site-packages。标准技能格式、Antigravity 固定来源、核心 wheel/sdist 内容与正文链接检查通过。固定清单技能包及本机技能安装的 5 个源码文件 SHA 全部复核通过。安装只是客户端技能文件就位，没有切换生产核心、配置账户、停用 MCP 或调用真实模型。固定 fake 大输出样本的完整响应为 242354 字节，模型可见摘要为 1241 字节；不代表 Token 或订阅节省率。客户端自动触发、其他宿主和真实收益未测，详细边界及证据见[本轮计划与结果](docs/standard-skill-plan-2026-09-30.md)。

核心包含 CLI/MCP、常驻执行、任务与原生会话引用、权限和审批、幂等、取消与对账、调度、备份恢复及可选代码质量流程。配置 v1/v2、SQLite schema 6、runner-control/v1 和 capability profile 保持兼容。各 Agent 按配置提供能力，由调用方组织任务流程。

2026-09-24 输入优化 R5：已按用户确认完成 Astra high 单对真实实验。Codex 原线程压缩、完成门禁和续跑现场通过，两组独立消费者测试各 9/9 通过，最终源码相同。压缩组累计输入比普通续接多约 14.8%，不宣称净节省，保持默认关闭；失败轮次用量保留。现场版本、授权、用量口径及剩余边界见[R5 结果](docs/token-optimization/R5-RESULTS-2026-09-24.md)。下文各阶段的未验证描述保留为当时状态；本轮未部署。

2026-09-24 输入优化 R4 后续：在原任务修复入口增加默认关闭的 Codex 原生压缩和新线程交接，保留幂等、权限、run/repair 上限和未决对账。安装版本仅只读核验 schema，真实模型能力和收益未测；其他真实后端、v2 共享准入及严格 Token 硬停未支持。全量离线 2523 项通过、4 项真实后端跳过；最终源码和独立 wheel 专项各 141 项通过。实现与验证见[增量记录](docs/token-optimization/STATUS.md)，真实调用仍待[R5 明确预算](docs/token-optimization/R5-EXPERIMENT.md)。未部署。

2026-09-24 输入优化 R0—R3：增加可选累计会话观察、可控文本/返回载荷 manifest、显式材料去重、有界诊断取回及事务化离线交接。默认优化关闭；不接管逐模型请求、不压缩或轮换真实会话，不把字节估算写成订阅收益。全量离线 2472 项通过、4 项真实后端跳过；收尾后源码专项 54 项、独立 wheel 相关用例 144 项通过，发行检查与安装后 CLI/MCP、重启重放及备份恢复通过。实际范围见[本轮状态](docs/token-optimization/STATUS.md)。未部署。

2026-09-24 阶段门禁批次：已把固定阶段要求接入外部 evaluate 和核心质量流程，接通 external_reported 部署回执、上游验收依赖队列、固定提交工作树槽位及运行/本地检查分离限额。新增只读直调日志索引和三路径配对样本汇总工具，导入不会接管原生会话、改变验收或重复计量。全量离线 2422 项通过、4 项真实后端跳过；最终源码与独立 wheel 的专项均为 116 项通过，发行检查及安装后 CLI/MCP、重启重放、备份恢复通过。详见[本批验证](docs/validation/stage-gates-dependencies-2026-09-24.md)和[调用方接口](docs/caller-orchestration.md)。原生接管、完整三路径执行对照、真实模型调优、A04 连续崩溃快照与自动 WIP 提交仍待后续；这批未部署。以下同日记录保留各批次当时的范围。

2026-09-24 按[直调复盘](docs/grokbuild-direct-retrospective-2026-09-24.md)实现首批改进：编码模式可显式启用受管原生续接，核对 CLI、身份、工作区及日志绑定；结构化识别 Grok 错误，并从持久 402 证据阻止同池后续派发；新增 CLI/MCP 受控局部检查，失败诊断可进入同任务修复。原有配置默认行为、修复上限、未知结果与独立审查语义保留。认证文件变动（含原生刷新）会保守阻止续接，旧版或直调会话不自动认领。

本批清洁环境全量离线回归为 2371 通过、4 个真实后端测试跳过。发行内容检查、Antigravity 固定来源核验、真实 CLI fake 修复闭环，以及独立 wheel 安装后的 231 项相关回归通过；安装环境首次缺少插件夹具构建依赖，补齐后该项单独复测通过。安装后的 CLI/MCP 两任务、94 条 schema 记录、重启重放及备份恢复通过。macOS 禁网、环境与原工作区隔离使用实际 OS 沙箱验证；Grok 续接和错误使用离线协议替身。详见[验证记录](docs/validation/grok-native-workflow-2026-09-24.md)。首批未部署或调用真实模型；当时 A03 缓存/异步及 A04—A08 保留待办，后续进展见下一段。

2026-09-24 继续实施：新增持久化异步检查与查询/取消、可选有界运行时指纹缓存、按工作区显式选定文件的私有检查点，以及分阶段证据汇总。客户端退出后核心继续检查；重启未决检查保留 unknown，重复键不重跑。检查点复用数据库产物存储和备份，保护派发前用户脏基线，查询核对漂移且不覆盖工作区。编码、消费者、独立审查、组合、部署分开呈现，外部自报不升级。全量离线回归 2390 项通过、4 项真实后端跳过；后续边界收敛后的源码及独立 wheel 安装专项各 61 项通过，包装与安装后 CLI/MCP、重启、备份恢复通过。完整口径见[后续记录](docs/validation/checkpoints-local-checks-2026-09-24.md)。这批尚未部署。完整项目/Linux 现场、部署证据接入、跨阶段自动验收门禁、连续崩溃快照与 A06—A08 仍待后续。

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

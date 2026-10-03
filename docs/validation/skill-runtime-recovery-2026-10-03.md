2026-10-03，用户反馈标准技能仍无法正常使用。现场核对发现 discover、resources、submit 和 wait 能调用当前实例；实际阻塞来自一条 10 月 1 日的 Grok 编码运行，它的原生记录包含回合上限事件、绑定会话的 cancelled end 和进程退出码 1，但核心仍保存为 paused / REMOTE_STATE_UNKNOWN，后端唯一并发名额一直被占用。后续任务排队后由原调用方取消，未被本次排查重派。

根因在 headless 读取器：完整排空 stdout/stderr 后先检查非零退出码，再解释已验证的 end。修复仅接受退出码 1 与明确未完成/取消的绑定结束事件组合，记为 failed / GROK_RUN_INCOMPLETE 或 cancelled；成功 end 配合非零退出、缺少 end、错会话、协议损坏、超时与读取未完成仍保持未知。

旧记录通过既有 task-reconcile 入口修复。核心先执行已有授权、配置和任务绑定检查，且原运行不能仍有活动工作线程；随后核对核心保存的回执 task_id、run_id、stage、prompt_attempted、delivery、failure_type、退出码、执行配置及结束原因。仅针对旧读取器的 nonzero_exit 误判重新确认终态，保留原回执并追加对账来源，不调用模型、不改成功验收、不创建替代运行。恢复完成后沿用核心原有释放名额和处理队列逻辑；不改变未知运行的占位规则。

技能增加 scheduler 查询占位任务，以及要求显式 task_id 的 reconcile。报告保留 terminal、unknown、observing；对账被受理不等于恢复或验收成功。scheduler 的新增引用按 task.get 授权过滤，旧核心没有 active_runs 时明确标记不可用。持续 queued 提示核对占位运行，不建议猜测 task-list、换键重派或提高并发绕过。

验证使用临时 HOME、临时状态库与离线原生协议进程。专项 200 项通过；完整隔离回归 2644 项通过、4 项真实后端测试跳过、0 失败。覆盖绑定 end 加退出码 1、错误绑定和不完整流保持未知、重启后的旧回执对账、排队任务恢复派发、重复对账不重复运行、占位引用权限过滤，以及真实常驻核心→CLI→复制技能的排队诊断和未决对账。Antigravity 三份固定来源校验通过。

本机已从历史 a13/8f3905e 切换到提交 `57de00e550fd4d32e91841a497ae261088b4c81b` 的独立修复构建，安装目录为 `0.1.0a14-57de00e-runtime-repair`。已发布 a14 标签及资产没有改变，仍不包含本次后续修复。新 wheel 的 102 个运行文件和技能包的 5 个源码文件与固定提交逐项核对一致；独立安装后的 CLI/MCP、重启及备份恢复验证通过，安装包恢复专项另有 21 项通过。pytest 缓存写入 `/dev` 的两条警告未影响测试结果。

实例切换保留配置 revision 7、SQLite schema 6、原生 HOME、旧安装和技能副本。切换前门禁只接受现场确认的那一条误判及其 operation，其他运行或未知项均拒绝；停止前后及新核心就绪后数据库快照完全一致，1445 项审查产物也完成备份核对。首次 prepare 因本轮证据目录权限过宽而拒绝，收紧为 0700 后重新准备成功，当时尚未停止或修改服务。切换成功后、业务对账前，回退入口预检通过；后续恢复不得以旧数据库备份覆盖当前业务状态。

通过已安装技能执行一次显式 reconcile 后，旧运行从 paused / REMOTE_STATE_UNKNOWN 变为 failed / GROK_RUN_INCOMPLETE，terminated=true；原任务仍由人工控制，acceptance=pending，没有被冒认为成功。现场 scheduler 回读 queued=0、inflight={}、active_runs=[]，占位已释放。数据库逐行比较仅原 run、对应 operation 和原 conversation 的 session 行发生变化，没有新增任务或运行，其余历史行完全一致。原运行除 status、terminated、error_code 和 native 新增对账来源外均未改变，原生会话、回执和用量保持原值。此前被调用方取消的排队任务仍为 cancelled / terminated=true，没有被重派。更新后的 discover、resources、status、scheduler、reconcile 均在本机常驻核心上回读通过。

私有复现、测试与部署证据位于本机 Asterun 数据目录的 skill-updates/20261003-runtime-repair。该目录保存原运行响应、专项及全套 JUnit、切换门禁、依赖版本清单、switch-plan/apply-result.json、skill-installation.json 与 live-recovery.json。实现已本地提交并安装，本轮未推送或公开发布。本次没有新增真实模型任务，排队恢复的离线协议证据不能替代真实模型产出验收。

# 外部主控的低开销工作流

调用方负责规划、运行验收命令和判断后续步骤，Asterun 保存任务、运行、版本绑定、验收记录与有限修复计数。平时只观察短状态；出现终态、审批或未决状态后，再读取需要的证据。以下 CLI 与对应 MCP 方法共用请求校验，不会因换入口扩大权限。

这些接口已实现，现有调用默认行为保持不变。部署与验证记录见源码仓库根目录的 `STATUS.md`；离线检查、真实后端、原生客户端和净节省率分别验收。

示例假设当前目录就是已配置工作区 `demo`，常驻实例使用 `.asterun/state`，实现后端名为 `grok-code`。将路径、后端和验收命令换成项目实际配置。`.asterun/caller` 保存本地请求与报告，应按项目运行数据规则管理；示例不会配置凭据、启动核心或更改后端权限。新参数需要客户端和常驻核心均已升级；旧核心拒绝 `compact` 时会返回错误，不会隐式重新读取完整正文。

## 提交一次，观察短状态

准备项目自己的 `task.md` 后提交，保存完整响应及其 ID。同一逻辑提交重试时保持幂等键和输入不变。

```sh
mkdir -p .asterun/caller
asterun --state-dir .asterun/state --connect task-submit \
  --workspace demo --backend grok-code --input task.md \
  --idempotency-key demo-feature-1 > .asterun/caller/submission.json
ASTERUN_TASK_ID="$(python3 -c 'import json; x=json.load(open(".asterun/caller/submission.json")); assert x["ok"], x.get("error"); print(x["ids"]["task_id"])')"

asterun --state-dir .asterun/state --connect task-get \
  "$ASTERUN_TASK_ID" --compact
asterun --state-dir .asterun/state --connect task-watch \
  "$ASTERUN_TASK_ID" --compact --no-events --timeout 60 \
  > .asterun/caller/observation.json
```

`task-get --compact` 对应 `task.get` 的 `compact: true`。它省略任务正文、完整工具日志和审批操作内容，保留任务验收状态、运行状态、取消与终止事实、错误、原生会话引用和已有用量。运行及审批摘要最多 1000 字符，`summary_truncated`、`summary_chars` 说明截断情况；`details` 指向完整的 `task.get`。用量只保留已知计数字段，合计最多 8 KiB、8 个模型；`usage_truncated` 和 `model_usage_count`、`model_usage_returned` 标明省略情况。标识和其它文本字段最多 1024 字节，超限字段列入 `omitted_fields`，不会截成另一个可用于恢复的标识。需要核实结果或处理审批时，去掉 `--compact` 读取完整响应。

`task-watch` 已经只在终态、待输入、待认证、暂停、未决状态或观察截止时返回。`--compact` 还会请求服务端先把事件页缩成元数据，再经 IPC 返回；单独读取事件可用 `task-events --compact`（MCP `task_events` 的 `compact: true`）。事件页最多 64 KiB，可能在达到 `page_size` 前分页；`has_more` 表示继续读取，`next_cursor` 只推进到实际返回的最后一条事件，无事件时保持原游标。`events_truncated` 表示完整正文或事件因投影、预算被省略，后续页使用返回的游标续查。`--no-events` 则完全不读取事件、不调用事件输出，也不推进事件游标。两者组合适合交给主控等待，不需要把每个原生工具调用送回模型。

观察结果中的 `reason=terminal` 仍需检查 `last_snapshot.run.status`，它也可能是 `failed` 或 `cancelled`，不表示验收通过。`waiting_input` 可能来自待批请求；`unknown` 保留未决语义，不能重新派发。`deadline` 只表示本次观察到期，CLI 退出码为 124，后台任务继续执行；中断观察退出码为 130，也不会取消任务。

继续观察时复用返回的任务 ID、`data.run_id` 和 `data.cursor`，通过 `--run-id`、`--cursor` 传入。非零游标必须带所属运行 ID；切换到新运行后才归零。使用 `--no-events` 返回的游标仍是原来已消费的位置，可以随后继续读取完整事件。不要把观察超时转成新的 `task-submit`。

## 在确定终态上取得目标绑定

主控确认当前运行已确定结束、没有其它写者后，选择与本次验收有关的源码、测试、依赖锁或配置文件。目标使用工作区内的相对文件路径；当前快照限制为 1–64 个不重复文件、内容合计最多 256 KiB，不递归接收目录。

```sh
asterun --state-dir .asterun/state --connect workflow-snapshot \
  "$ASTERUN_TASK_ID" \
  --target src/main.py --target tests/test_main.py --target pyproject.toml \
  > .asterun/caller/snapshot.json
```

`workflow.snapshot` 返回 `task_id`、`run_id`、`revision`、`input_hash`、`target_paths`、`target_hash` 和文件 manifest，不返回源码正文，不执行构建或测试。可用 `--run-id` 指定已经观察到的运行，拒绝误取后续运行。目标摘要包含所选文件的内容、模式、缺失状态，以及 Git 工作区的 HEAD；不会创建隔离 checkout 或锁住工作区。调用方仍须控制写者，或在自己保全的固定副本中验收。未选文件不在这一目标摘要的覆盖范围内。

## 把真实验收结果绑定回任务

外部报告的顶层字段严格限定为 `task_id`、`run_id`、`input_hash`、`target_hash`、`checks`。每项检查只有 `name`、布尔值 `passed` 和字符串 `detail`；名称须唯一，检查数量为 1–64 项。单项名称最多 200 字符，说明最多 4000 字符。报告必须是工作区内最多 256 KiB 的普通文件，使用规范相对路径及该文件原始字节的 SHA-256；符号链接、重复 JSON 键或版本不匹配会被拒绝。

以下片段读取真实 `workflow-snapshot` 响应，运行项目测试，把退出结果写为外部报告，并机械生成评价请求，避免手写或误抄绑定。命令只是示例验收范围；主控应使用项目已经确定的检查，不把一个测试文件的通过写成完整项目通过。测试输出保存为日志，不整段塞回模型。超时同样保存为失败；如果测试还派生了子进程，须由项目验收执行器确认这些进程已收束，再进入修复。

```python
from pathlib import Path
import hashlib
import json
import subprocess

directory = Path(".asterun/caller")
envelope = json.loads((directory / "snapshot.json").read_text())
assert envelope["ok"], envelope.get("error")
target = envelope["data"]
binding = {
    "task_id": target["task_id"],
    "expected_run_id": target["run_id"],
    "expected_revision": target["revision"],
    "expected_input_hash": target["input_hash"],
    "target_paths": target["target_paths"],
    "expected_target_hash": target["target_hash"],
}

command = ["python3", "-m", "pytest", "tests/test_main.py"]
with (directory / "pytest.log").open("wb") as log:
    try:
        completed = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=300)
        passed = completed.returncode == 0
        detail = f"exit_code={completed.returncode}; log=.asterun/caller/pytest.log"
    except subprocess.TimeoutExpired:
        passed = False
        detail = "300秒内未完成；log=.asterun/caller/pytest.log"

report = {
    "task_id": target["task_id"],
    "run_id": target["run_id"],
    "input_hash": target["input_hash"],
    "target_hash": target["target_hash"],
    "checks": [{"name": "tests/test_main.py", "passed": passed, "detail": detail}],
}
report_path = directory / "external-report.json"
raw = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
report_path.write_bytes(raw)
request = {
    **binding,
    "checks": [{
        "kind": "external_report",
        "path": report_path.as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }],
}
(directory / "evaluate-request.json").write_text(
    json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
```

```sh
asterun --state-dir .asterun/state --connect workflow-evaluate \
  "$ASTERUN_TASK_ID" --request .asterun/caller/evaluate-request.json \
  > .asterun/caller/evaluation.json
```

`checks[].kind=external_report` 会核对报告内容摘要、任务、当前运行、原任务输入和所选文件版本。结果中的 `checks[].source=external_reported` 表示调用方报告的检查结论；Asterun 没有代执行这些检查，也没有证明报告作者是独立审查者。报告不接受自报的 `actor`、`review` 或 `source` 字段。评价成功的响应仍须检查 `data.acceptance`，不能只看信封 `ok`。

同一固定验收请求还可以使用已有的 `file_exists`、`file_contains` 检查，但它们的 `path` 必须明确列在本次 `target_paths` 中。不能绑定少数文件，却用目标之外的可变文件让验收通过。

配置或请求中的 `require_review` 仍有效，并通过任务的 `external_require_review` 持久保留。后续评价省略它、传入 `false`，或先执行一轮修复，都不能清掉已经提出的审查要求。外部报告全部通过也不能替代受信的审查证据；缺少审查时验收保持 `pending` 并暂停自动推进，不会因审查后端“可调用”而通过。已启动的核心质量流程由它自己的固定检查与编排器管理，不接受外部请求改写成更低的验收条件。

报告或所选目标后续变化时，旧验收不再适用于当前版本，读取任务会保留失效事实并使验收回到 `pending`。每轮都保留原报告、日志及绑定；不要修改已经作为证据引用的文件来覆盖历史失败。

## 在原任务中进行有限修复

可先运行[完整离线 CLI 范例](../examples/caller-workflow/README.md)，核对同一任务、多次运行、报告失败、幂等修复及目标漂移失效。

需要修复时，主控先根据具体失败准备 `.asterun/caller/repair-instructions.md`，写清改动范围、原失败与通过条件。修复指令不替换原任务目标、不授予额外权限；绑定必须来自要修复的当前运行及当前文件版本。若验收后目标已变，重新取得快照并核实缺陷，不能直接沿用旧摘要。

以下片段将同一快照转成修复请求。示例的 `max_repairs=2` 表示整个任务的累计上限，不是本次再追加两轮；实际仍取请求与配置中较小的上限，并受每任务运行次数限制。主控应只为已确认的缺陷发起修复；已有运行必须确定结束，任务不能处于暂停或人工接管状态。达到上限后交接现有结果，不通过新建任务规避上限。

```python
from pathlib import Path
import json

directory = Path(".asterun/caller")
envelope = json.loads((directory / "snapshot.json").read_text())
assert envelope["ok"], envelope.get("error")
target = envelope["data"]
instructions = (directory / "repair-instructions.md").read_text(encoding="utf-8")
assert instructions.strip(), "需要基于已确认失败填写修复指令"
request = {
    "task_id": target["task_id"],
    "expected_run_id": target["run_id"],
    "expected_revision": target["revision"],
    "expected_input_hash": target["input_hash"],
    "target_paths": target["target_paths"],
    "expected_target_hash": target["target_hash"],
    "instructions": instructions,
    "max_repairs": 2,
    "idempotency_key": "demo-repair-" + target["run_id"] + "-1",
}
(directory / "repair-request.json").write_text(
    json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
```

```sh
asterun --state-dir .asterun/state --connect workflow-repair \
  "$ASTERUN_TASK_ID" --request .asterun/caller/repair-request.json \
  > .asterun/caller/repair.json
```

同一修复请求因响应丢失而重试时，复用完全相同的请求与幂等键；不能给未知结果换键重派。同键同请求读回原操作，不再增加修复计数；同键但修复指令或绑定不同会被拒绝。新修复使用原 `task_id`、新增 `run_id`，累计 `repair_count` 和 `run_count`，原输入及历史运行继续保留。后续按新运行重新观察、快照和验收，旧通过结论不能带到新轮。

兼容旧版修复记录时，只有不含新增指令或版本绑定的旧式请求可以读回旧意图；旧记录未保存完整请求，无法追溯核对当时的 `max_repairs` 参数。该兼容路径也不会重新派发或增加预算。

同一个 Asterun 任务不代表复用了 Grok 原生会话。原生续接取决于适配器能力与实际绑定，须检查原生引用和 `native_resumed` 的证据；不能把任务关系或相同 conversation ID 当成原生上下文已经复用。

## 受控局部检查

`workflow-check` / MCP `workflow_check` 只允许调用 `workflow.local_checks` 中预先配置的检查名，请求不能传入命令或环境变量。例如：

```json
{
  "workflow": {
    "local_checks": {
      "compile": {
        "argv": ["/absolute/python3", "check.py"],
        "inputs": ["check.py", "src/example.py"],
        "timeout_seconds": 30,
        "runtime_roots": ["/absolute/python-runtime"],
        "result_file": "counts.json"
      }
    }
  }
}
```

可执行文件使用绝对路径，参数数组固定，不经 shell 拼接；`runtime_roots` 仅允许只读工具链/依赖目录，不能覆盖原工作区，不能填用户凭据目录。检查在只包含 `inputs` 的临时副本中运行，全部输入必须列入快照 `target_paths`。文件限额沿用 64 个文件、总计 256 KiB。运行时清空继承环境，HOME/TMPDIR 指向临时目录，使用 macOS sandbox-exec 或 Linux bubblewrap 禁网；缺少隔离工具时拒绝执行。超时最多 60 秒，结束后清理进程组，输出摘要有界，前后源码摘要不一致则不返回可用报告。默认保持同步调用；常驻核心可使用下面的异步入口，避免检查占用请求线程。

纯编译可省略 `result_file`，以退出码和超时判断；测试检查应配置结果文件。该文件必须由运行器新生成，含 `passed`、`failed`、`skipped`、`load_errors` 四个非负整数。文件缺失/损坏、有失败/加载错误或没有任何通过的测试均不能通过；未知数量返回 null，不猜测日志中的测试数。

从 `workflow-snapshot` 构造与外部验收相同的完整版本绑定，再增加 `check_name`：

```sh
asterun --state-dir .asterun/state --connect workflow-check \
  "$ASTERUN_TASK_ID" --request .asterun/caller/check-request.json
```

响应包含退出码、超时、测试数量、诊断和前后摘要，以及可交给既有 `external_report` 验收的 `report` 对象。由调用方保存报告并计算文件 SHA-256，再使用完整版本绑定调用 `workflow-evaluate`。失败详情会有界地进入同任务修复提示；自测不会自动更改验收、满足独立审查或启动修复。原项目的完整依赖构建仍由调用方在授权环境执行并导入报告；不要把局部输入检查说成完整项目测试。诊断缓存默认关闭，可为固定检查显式配置 `cache: true`。

## 后台检查、检查点和分阶段证据

常驻核心收到完整检查绑定及 `"async": true, "idempotency_key": "compile-round-1"` 后立即返回 `job_id` 和状态。同一运行中，相同幂等键复用原意图；异输入拒绝。调用客户端退出后，常驻核心继续检查并主动保存结果。每个实例最多同时执行两个局部检查；占满时返回 `RATE_LIMITED`，不隐式排队。每次运行最多保存 32 个检查意图，重复请求应复用幂等键。同步入口继续返回原来的报告字段，异步结果位于查询响应的 `result`。

```sh
asterun --state-dir .asterun/state --connect workflow-check-status \
  "$ASTERUN_TASK_ID" --run-id "$ASTERUN_RUN_ID" --job-id "$ASTERUN_CHECK_ID"
asterun --state-dir .asterun/state --connect workflow-check-cancel \
  "$ASTERUN_TASK_ID" --run-id "$ASTERUN_RUN_ID" --job-id "$ASTERUN_CHECK_ID"
```

MCP 对应 `workflow_check_status`、`workflow_check_cancel`。检查状态与实现运行状态分离。`completed` 表示检查执行结束，仍须读取 `result.passed`、加载错误和超时；`stale`、`failed`、`unknown` 或 `cancelled` 均不能用作通过证据。取消先保存意图，再终止检查进程组，与成功竞争时不公布成功报告。核心重启后未确认意图变为 `unknown`；重复键不重跑，确认旧执行方已退出后才可显式新建检查。检查在临时副本内执行，即便检查进程异常遗留也不会写回项目。

`cache: true` 仅在同任务、同运行、完整目标摘要、输入、配置及固定命令相同时复用已完成的诊断，成功和失败均可复用，超时、取消和未知结果不缓存。运行时指纹覆盖沙箱允许读取的目录与符号链接目标的设备、inode、mode、size、mtime、ctime，并加入执行文件、检查实现字节、Python 与系统版本；不是只比较文件时间戳。运行前后指纹改变会使结果失效。枚举上限为 100,000 个节点、每次两秒；无法完整枚举、不可读或特殊文件导致自动关闭缓存，不猜测命中。该优化适用于元数据可靠的本地文件系统；网络文件系统或不可靠元数据环境保持关闭。响应明确返回 `cache_hit`、`cache_available` 和来源检查 ID；复用结果依然不修改 acceptance。保存过的报告表示当时环境，更新工具链后须重新请求检查，不能把旧查询当成刚执行。

自动检查点需按工作区显式配置文件范围，例如 `workflow.checkpoint_paths: {"demo": ["src/main.py", "tests/test_main.py"]}`。派发前保存选定文件的实际内容、模式、HEAD 和该范围的 dirty 清单，以用户当前脏内容为基线。结束时保存新内容、缺失状态、新增/删除文件清单和文本差异；完整内容与模式是恢复依据，文本差异只供比较。最多 64 个 UTF-8 文件、256 KiB，未选文件不在恢复范围。私有产物复用数据库的内容摘要存储，随 `state-backup` 一并保存，普通任务和 compact 响应仅带引用与下一动作；配置启用后，备份包含这些选定源码，应按私有项目数据保存。

```sh
asterun --state-dir .asterun/state --connect workflow-checkpoint \
  "$ASTERUN_TASK_ID" --run-id "$ASTERUN_RUN_ID"
```

MCP 为 `workflow_checkpoint`。加 `--include-artifact` 可读取不超过 512 KiB 的当前产物；更大原件仍保留在状态库备份中。查询核对当前源树和身份，返回 `source_matches`、`recovery_ready`、最近检查引用和下一动作。这里的 ready 只表示所选文件产物与当前树一致且运行已确定结束，不表示已核对原生续接能力。基线或终态产物落盘失败不会触发重复派发；崩溃后的未决运行仍须对账。查询不会覆盖文件、写 Git 索引、提交、派发或自动恢复原生会话。需新建会话或续接时，继续使用既有绑定与权限检查。任意进程硬崩溃期间的实时编辑尚无连续保存保证；只能使用最后成功写入的检查点。

局部检查还可配置 `stage: "coding"`（默认）、`"consumer"` 或 `"integration"`。分别配置真正覆盖消费者与组合行为的命令；改阶段名称不能替代对应测试。以 `workflow-snapshot` 的完整绑定调用 `workflow-evidence TASK --request binding.json`，MCP 为 `workflow_evidence`。它分别呈现编码、消费者、独立审查、组合和部署状态，汇总同阶段的全部已配置检查；未跑、旧版本或缺项保持未验，失败按文件和错误类别压缩。独立审查仅取既有核心质量流程的绑定证据，fake 审查明确为 simulated；外部自报不会升级。部署可导入固定外部回执，但来源始终为 external_reported，不能因本地通过宣称已部署。此入口只汇总证据，不改变 acceptance；通过下文 stage_gates 将必需阶段接入 evaluate 和核心质量流程。

## 汇总整个完成过程的用量

```sh
asterun --state-dir .asterun/state --connect usage-report --task-id "$ASTERUN_TASK_ID"
asterun --state-dir .asterun/state --connect usage-report --workspace demo
asterun --state-dir .asterun/state --connect usage-report \
  --task-id "$ASTERUN_TASK_ID" --include-runs > .asterun/caller/usage.json
```

也可使用 `usage-report --conversation-id CONVERSATION_ID` 汇总该会话关联的任务；多个筛选条件同时提供时取交集，至少提供一个范围。对应 MCP 工具为 `usage_report`（应用方法为 `usage.report`），字段使用 `task_id`、`workspace`、`conversation_id`、`include_runs`、`page_size`、`cursor`。默认返回完整查询的总计，以及首批分组和任务状态；只有 `--include-runs` 才附逐运行原生统计。

明细默认每类最多 20 条，可用 `--page-size 1` 至 `--page-size 100` 调整；每页明细还受 128 KiB 字节预算限制，因此可能提前分页。`pagination.collections` 分别列出任务、分组和运行的 `total_items`、`offset`、`returned_items` 与 `omitted_items`；`pagination.has_more=true` 时，保持相同范围和 `--include-runs` 选项，将 `pagination.next_cursor` 原样传给 `--cursor`。`totals` 每页都覆盖完整授权查询，不能再把多页总计相加。游标绑定本次报告的内容摘要；用量、任务状态或范围变化时会拒绝旧游标，此时省略游标重新查询。翻页仍会检查全部任务的读取权限。

单条明细超过 16 KiB 时，优先省略该条的 `native_usage`；仍过大时只返回可容纳的引用。`detail_omitted` 明确给出省略原因、字段、原明细字节数及 SHA256，不把省略值当作零或完整明细；需要完整超大原始统计时读取本地持久记录。省略仅影响返回的明细，不改变总计，也不修改原生记录。

不使用 `--connect` 的独立 `usage-report` 只读打开已存在且结构完整的当前 schema 状态库；旧版、未来版本或缺表都会被拒绝，不创建、修补或迁移数据库，升级须走正常备份维护流程。报告内任务状态来自已保存快照，`acceptance_revalidated=false` 表示此次没有重新核对外部验收文件；要确认当前验收状态，另用 `task-get`。

报告按原 task 下保存的全部运行计量，包含失败、取消和修复轮，不只看最后一次成功。缺失值保持未知，部分用量保留不完整标记；原生费用不是已核验账单，`billed_usage_verified` 保持 `false`。输入缓存与输出内的 reasoning 是不同统计维度，不能把父项与子项重复相加。Codex Runtime 会保留已收到的非空 `token_usage` 通知，标记 `thread_cumulative`；没有通知时保持未知。线程累计计数不能直接当作每轮增量反复累加；不可加总的范围与原因会单列。

Claude 单次调用的美元费用独立于 token 来源计入运行汇总，依据原生一次性快照及费用上报标记识别，不能只凭后端名称推断。新快照用 `cost_reported`、`cost_source` 区分原生确报零费用与未报告费用；旧快照中的正数仍可计入，旧版默认零值无法证明免费，保留原始值但在规范统计中记为未知。累计会话或线程费用不会作为单次费用重复相加。

衡量搭配效果时，另行记录主控模型的用量、等待、验收与返工，按相同任务范围和质量门比较整个完成过程。Asterun 的输出变短、Grok 承担编码或缓存命中增加，都不能单独证明 Astra 节省了某个百分比；没有可比较的实测对照时，节省率应保持未测量。


## 固定阶段门禁与外部回执

可在配置中加入以下字段；未配置 stage_gates 的既有流程保持原行为。阶段策略随任务持久保存，只能加强；删除配置、检查名或省略 evaluate 的 checks 不能取消已有要求。配置 revision 改变后仍遵守原有任务绑定规则，不能把旧任务无声迁到新配置。

```json
{
  "workflow": {
    "stage_gates": {"coding": "core", "consumer": "core", "independent_review": "core"}
  },
  "scheduler": {"max_concurrency": 3, "per_backend_concurrency": 3, "local_check_concurrency": 2}
}
```

这里的 3 路运行、2 路本地检查只是实验配置，未证明最优。全局运行上限可选 1—32，未指定时保持原先每后端限制；本地检查上限为 1—8，默认 2。后台实施/审查运行共用全局限额，并继续服从账户池、工作区单写者和额度熔断。后台检查达到上限时返回繁忙，不创建无界线程。

coding、consumer、integration 使用对应 stage 的已配置局部检查；缺检查名不能凭空通过。independent_review 只能取核心质量流程的真实审查记录，fake 审查保持 simulated，不能满足该门禁。consumer、integration 可显式允许 external_reported；已知本地失败仍优先，外部成功不能盖住失败。deployment 目前只接受显式允许的 external_reported，不提供核心自动部署证明。

核心质量流程审查完成后，缺阶段证据时进入 awaiting_stages；可继续提交同版本检查或回执，再轮询推进。等待期间不自动反复调用模型。已完成的审查目标仍有效时，补齐被删除的同版本回执也不必重新审查。源码变化、上游依赖变化或报告篡改仍使验收失效。外部主控流程在导入后显式重新 evaluate。

`workflow-attest TASK --request request.json` / MCP `workflow_attest` 接收与 workflow-check 相同的完整目标绑定，额外提供 `stage`（consumer/integration/deployment）、`idempotency_key` 和 `report: {path, sha256}`。路径为当前任务工作区内的相对普通文件，最多 256 KiB，不允许符号链接。回执正文严格使用以下字段：

```json
{
  "task_id": "task-reference",
  "run_id": "run-reference",
  "input_hash": "snapshot-input-hash",
  "target_hash": "snapshot-target-hash",
  "revision": 1,
  "stage": "deployment",
  "environment": "staging",
  "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "release_id": "release-reference",
  "checks": [{"name": "product-probe", "passed": true, "detail": "调用方现场检查说明"}]
}
```

示例中的引用和哈希须替换为实际快照值与产物摘要。checks 每项可附 `counts: {passed, failed, skipped, load_errors}`；加载错误非零或没有通过项不能验收为绿。导入绑定运行、输入、源码版本和配置 revision；每次汇总会重新读取文件核对 SHA。环境、release_id 和产物摘要来自调用方自报，并不证明核心连接了该环境或检查了产物。返回始终保留 `independently_verified: false`、`deployment_executed: false`。策略允许回执满足门禁也不会将 `all_stages_verified` 改成 true。

## 工作树槽位与依赖任务

先在配置中登记源仓库与独立的空目标目录，目标使用 `allow_non_git: true`。调用 `workspace-materialize --request request.json` / MCP `workspace_materialize`：

```json
{"source_workspace": "source", "workspace": "worker-a", "commit": "0123456789012345678901234567890123456789"}
```

commit 必须替换为源仓库中已存在的完整提交 SHA。操作使用本地 `git worktree add --detach`，不取远端、不复制源工作区脏文件、不提交或合并。已有槽位只有仓库、提交和干净状态全部一致才幂等复用；非空目录、脏工作树、相关活动/排队/未决任务或自定义 checkout 过滤器均拒绝。操作超时会保留目录要求核对，不自动删除或重建；这是受控槽位物化，不是任意路径创建或跨主机隔离。

`task-submit --request request.json` 可增加：

```json
{"workspace": "combined", "text": "组合两个工作任务的产物并执行集成检查",
 "depends_on": ["task-worker-a", "task-worker-b"], "required_stages": ["integration"],
 "idempotency_key": "combine-once"}
```

最多 16 个直接依赖、128 个祖先，必须引用既有任务，各下游工作区与上游独立且不嵌套。依赖与 required_stages 参与幂等输入摘要；required_stages 只能增加核心门禁。下游沿用既有持久队列，等待上游固定版本验收通过；单有 run succeeded 不足以放行。排队项不阻塞后面可运行的独立任务。派发时固定每个上游的 run/input/target/revision，后续上游修复、源码漂移或权限撤销使下游验收不能保持 passed。

依赖声明不会自动合并文件。组合任务仍须在独立槽位中按授权范围整合产物，并完成 integration 检查；两个 worker 都通过不代表组合通过。额度熔断继续阻止新依赖任务启动；既有未决运行仍需对账。

## 只读附加直调历史

`session-import --request request.json` / MCP `session_import` 向已确认终态的既有任务附加日志观察。请求字段为 `task_id`、已配置 Grok `backend`、规范 UUID `session_id`、`summary_sha256` 和 `updates_sha256`。后端必须配置显式 home，输入不能指定其它源路径。读取其 sessions 下与任务工作区对应的 summary.json / updates.jsonl，要求 summary.info.id/cwd 精确匹配；绑定不明的旧日志拒绝导入。

每个文件最多 16 MiB，updates 每行最多 256 KiB。导入前后核对指定摘要，拒绝符号链接、重复 JSON 字段、不完整行和内容漂移。每个任务最多 16 份观察；同会话重复请求幂等，跨任务重复附加拒绝。只保存日志 SHA、完成回合事件数量和白名单用量字段，不存原始消息或隐藏推理，不写原生目录，也不生成受管会话 marker。

结果标记 external_observed、usage_scope=unknown、ownership_verified=false、resume_supported=false。最后一次可识别的用量报告作为观察保留，不假设它是单轮或累计值，不加入 usage-report 总计，因此不重复计量；未知值不写成零。导入不把历史 exit0 变为 acceptance passed，也不调用模型。原生接管尚未开放：现有受管 resume 仍要求自己的身份、CLI、凭据代际和日志绑定。读取历史不授予执行权，不能用导入绕过活动 PID 或单一执行权核对。

## 实验性输入优化与离线交接

`workflow.input_optimization` 默认省略（等价于 off）。先在隔离配置选择 `{"mode":"observe","scopes":["dispatch_prompt","caller_result"],"max_dispatch_bytes":16000}`。observe 保存字节数、SHA 和候选估算，实际投递与 task.get 返回正文不变，不保存另一份原始载荷。`enforce` 才对投递材料去重并自动使用既有 compact 返回。`dispatch_prompt` 的 enforce 必须显式指定 `max_dispatch_bytes`（1—1048576）；它是文本字节限制，不是模型上下文容量或严格原生 Token 预算。unsupported scope、旧式压缩/轮换布尔开关和严格 Token 配置拒绝解析；下文的显式上下文策略单独配置。

`task-submit --request request.json` 可显式附加 `dispatch_materials`，每项为 `{"path":"src/parser.py","sha256":"<当前文件的64位小写SHA256>","start_line":1,"end_line":20}`，最多 16 项。以完整原目标/限制为 Mandatory，再附加当前文件的选定行；同包中路径、范围、版本全部相同的材料才允许去重。不同路径即使内容相同也保留，原 Task.text 不被摘要覆盖。off/observe 会按显式请求附加全部材料；enforce 移除重复项。每次派发重新检查权限、路径和内容，不缓存“模型已读过”的假设。所选文件读取复用 snapshot 的文本、大小、符号链接限制；材料变化、保存失败或 Mandatory 超限会阻塞。修复继续绑定原材料版本，不能静默拿过期材料重派。

这条实验投递路径仅用于核心 v1 文本任务；v2 固定执行计划、typed capability、固定读取和独立审查的变换拒绝启用。它没有接管原生内部工具循环。原生客户端版本、逐模型请求与订阅扣量仍可能未知。

新增 CLI `workflow-payload`、`workflow-check-detail`、`workflow-handoff-create`、`workflow-handoff-read` 均使用 `--request request.json`；MCP 对应 `workflow_payload`、`workflow_check_detail`、`workflow_handoff_create`、`workflow_handoff_read`。共同字段为 `task_id` 和 `expected_run_id`，必须指向当前运行，每次重新检查权限和工作区绑定。查询不会推进队列或派发；runner-control 管理的任务不走这些 legacy 接口。

`workflow-payload` 读取保存的 manifest 与适配器能力证据。`dispatch_prompt` 只覆盖原生入口文本；`caller_result` 只覆盖 task.get 的 data 正文，以 UTF-8 紧凑 JSON 排序序列化（不包含外层 Envelope、传输包装或原生工具 schema）。`estimated_tokens=ceil(serialized_bytes/4)` 是粗略估算，没有 tokenizer 精度承诺。`candidate_omissions` 表示候选删除数，observe 并未实际删除。策略和适配器文件摘要用于辨别采集版本；元数据不在普通 task.get 中递归回显。

`workflow-check-detail` 增加 `job_id`、可选 `offset`（从 0 开始的 Unicode 字符位置）和 `limit`（1—2000，默认 1000）。只切片已经保留、版本匹配的诊断；另返回确定性错误分组、exit code、counts、截断与长度。摘要复用已有脱敏器，是派生件；`summary_sha256` 指向摘要，`sha256_of_retained_diagnostic` 指向原保留诊断。原诊断仍需当前任务、workspace.read、workflow.evaluate 和 process.execute 授权。原始日志仅沿用既有有界尾部，`full_log_retained=false`，不能通过分页取回未保存的完整日志；不自动访问日志中的路径或 URI。脱敏匹配不构成完整秘密识别保证。

`usage-report --include-observations` 可查看未归属会话观察。Codex 累计计数保留已观测最大值和重置/乱序疑点，不求不可信的差值，不分摊给 run；身份不完整时不跨任务合并。同一 session 的导入与实时观察均不额外加入运行总量；没有发生时间的观察归入 unknown_occurrence，采集时间不是消费发生时间。旧记录不补造采集时间，真实失败/重试的已知原生运行费用继续保留。旧用量分页/总计保持原口径，覆盖 controller/not_observed、native_requests/unknown、subscription_quota_effect/unknown。

创建离线交接请求：`{"task_id":"task-id","expected_run_id":"run-id","expected_handoff_revision":0,"target_paths":["src/parser.py"]}`。创建结果返回 blob SHA、revision 和离线准备状态；后续更新须提交当前 revision。SQLite 在同一事务内写 blob 和 CAS 指针，已存在有效版本不会被失败写入替换。内存替身有 CAS，旧 JSON 文件存储不支持原子交接，明确拒绝。复用已有 blob 保留与备份策略，没有新增 TTL 删除路径。

读取请求：`{"task_id":"task-id","expected_run_id":"run-id","expected_handoff_revision":1,"reconstruct":true}`。复核原 Task/Run/事件/审批、策略、源文件和验收证据；漂移、引用缺失或 schema 不支持时失败，不回退旧通过结论。原目标和限制从权威 Task 完整加载，所选文件重新读取，保留审查与修复/运行上限、未决动作。未知运行或待审批可生成离线材料，但不会标成已准备接管。快照只存引用、哈希与必要状态，不复制完整聊天或源码，也不创建、恢复或派发原生会话。

回滚实验策略时，将 mode 改回 off 并按既有配置 revision 流程应用；关闭新投递/返回变换，不删除旧 manifest、交接或验收证据，不撤销外部动作。默认配置和生产实例不会由这些接口自动变更。

## 实验性原生上下文切换

仅核心 v1 常驻执行器的普通 Codex 文本任务开放显式策略，默认关闭；同步兼容入口明确拒绝。隔离配置可设 `workflow.input_optimization` 为 `{"mode":"enforce","context_strategies":["native_compact","handoff_fresh"]}`。这不启用自动阈值轮换，也不改变原生工具权限。v2 共享准入、typed capability、固定只读、质量流程持有的任务和其他真实后端尚未验证，明确拒绝，不更换后端。fake 只用于核心状态机验证。

在原任务的当前运行已确认终止、操作已结束、审批及本地检查均无未决项后，先创建上述离线交接包，再调用既有 `workflow-repair TASK --request repair.json`（MCP 为 `workflow_repair`）。请求示例为 `{"task_id":"task-id","expected_run_id":"source-run-id","expected_handoff_revision":1,"context_strategy":"native_compact","idempotency_key":"fixed-successor-key"}`。存在外部验收或附加修复指令时，仍须提交既有 snapshot 的完整运行、配置、输入和目标绑定。重复相同键只返回原意图；相同键更改内容拒绝。

`native_compact` 续接原线程，核对安装二进制和导出 schema 后请求原生压缩。RPC ACK 只表示受理；必须观察同线程、同新轮次的 contextCompaction 产物与完成事件，再核对原生持久历史，才发送工作提示。`handoff_fresh` 创建新线程，完整携带原目标、约束、审查要求、全部 finding、选定文件和预算材料；它明确不是原生 resume。完整材料超过原修复提示限额时拒绝，不截断必需信息。

两条路径都在原 Task 上原子提交修复意图、后继 Run 和计数。派发前复核源运行、事件、策略、交接引用、所选源码及旧验收证据，派发后不把旧验收复制为新运行通过。相同任务的竞争后继由事务内 CAS 拒绝；原 run/repair 上限保留，已占用次数不因断连或未知结果而退还。没有新建预算账本，不提供原生逐请求 Token 硬上限。

断连、压缩失败、超时、存储失败或取消留下未确认阶段时，停止后继工作提示，保留原线程和交接证据，进入 pending_reconcile。对账不会把旧工作轮次或压缩完成当作新任务成功；prompt_started 之前不自动补发，之后只核对保存的新轮次。可能已经发生的远端动作需要人工核验，不能换幂等键重试来绕过未决状态。停止观察也不代表远端动作已取消。

原生累计用量仍按会话观察保存，不从压缩前后计数猜测新 run 消耗。能力记录的 installed_schema_only 只证明该安装导出了所需协议，不证明真实模型成功压缩、权限/profile 一致或订阅节省。默认回归使用离线协议进程；真实账户、外部原生写者与模型效果按固定安装版本和 profile 分别验收，单次现场成功不构成全局能力声明。关闭 context_strategies 或 mode=off 可阻止新的切换，不删除旧证据或撤销已发出的原生动作。

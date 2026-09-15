# 外部主控的低开销工作流

调用方负责规划、运行验收命令和判断后续步骤，Asterun 保存任务、运行、版本绑定、验收记录与有限修复计数。平时只观察短状态；出现终态、审批或未决状态后，再读取需要的证据。以下 CLI 与对应 MCP 方法共用请求校验，不会因换入口扩大权限。

这些接口已实现，现有调用默认行为保持不变。部署与验证记录见 [STATUS](../STATUS.md)；离线检查、真实后端、原生客户端和净节省率分别验收。

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

`task-get --compact` 对应 `task.get` 的 `compact: true`。它省略任务正文、完整工具日志和审批操作内容，保留任务验收状态、运行状态、取消与终止事实、错误、原生会话引用和已有用量。运行及审批摘要最多 1000 字符，`summary_truncated`、`summary_chars` 说明截断情况；`details` 指向完整的 `task.get`。需要核实结果或处理审批时，去掉 `--compact` 读取完整响应。

`task-watch` 已经只在终态、待输入、待认证、暂停、未决状态或观察截止时返回。`--compact` 还会把输出的事件页缩成元数据；`--no-events` 则完全不读取事件、不调用事件输出，也不推进事件游标。两者组合适合交给主控等待，不需要把每个原生工具调用送回模型。

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

## 汇总整个完成过程的用量

```sh
asterun --state-dir .asterun/state --connect usage-report --task-id "$ASTERUN_TASK_ID"
asterun --state-dir .asterun/state --connect usage-report --workspace demo
asterun --state-dir .asterun/state --connect usage-report \
  --task-id "$ASTERUN_TASK_ID" --include-runs > .asterun/caller/usage.json
```

也可使用 `usage-report --conversation-id CONVERSATION_ID` 汇总该会话关联的任务；多个筛选条件同时提供时取交集，至少提供一个范围。对应 MCP 工具为 `usage_report`（应用方法为 `usage.report`），字段使用 `task_id`、`workspace`、`conversation_id`、`include_runs`。默认返回总计、按后端及模型等维度的分组和任务状态；只有 `--include-runs` 才附逐运行原生统计，避免日常查询搬运明细。

不使用 `--connect` 的独立 `usage-report` 只读打开已存在且结构完整的当前 schema 状态库；旧版、未来版本或缺表都会被拒绝，不创建、修补或迁移数据库，升级须走正常备份维护流程。报告内任务状态来自已保存快照，`acceptance_revalidated=false` 表示此次没有重新核对外部验收文件；要确认当前验收状态，另用 `task-get`。

报告按原 task 下保存的全部运行计量，包含失败、取消和修复轮，不只看最后一次成功。缺失值保持未知，部分用量保留不完整标记；原生费用不是已核验账单，`billed_usage_verified` 保持 `false`。输入缓存与输出内的 reasoning 是不同统计维度，不能把父项与子项重复相加。Codex Runtime 会保留已收到的非空 `token_usage` 通知，标记 `thread_cumulative`；没有通知时保持未知。线程累计计数不能直接当作每轮增量反复累加；不可加总的范围与原因会单列。

衡量搭配效果时，另行记录主控模型的用量、等待、验收与返工，按相同任务范围和质量门比较整个完成过程。Asterun 的输出变短、Grok 承担编码或缓存命中增加，都不能单独证明 Astra 节省了某个百分比；没有可比较的实测对照时，节省率应保持未测量。

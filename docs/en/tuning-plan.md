[中文](../tuning-plan.md) | [English](tuning-plan.md)

# Asterun tuning and acceptance measurement plan v0.1

Plan date: 2026-09-07. This document preserves experiments that have not yet been run, along with initial sample sizes and candidate targets. The numbers guide experimental planning; measured performance, SLAs, and model-spending budgets require separate confirmation. See the [installation guide](install.md) for current runtime settings and the [development plan](development-plan.md) for dependencies.

## Objective and sequence

Measure the work required to complete an accepted task, then reduce waiting, rework, resource use, and human intervention. Keep acceptance scope, model quality, authorization, and persistence requirements constant, and record errors and recovery outcomes in full.

Establish a baseline and instrumentation in A1. Measure state, process, and event overhead in A2, then compare workflows and backend combinations in A3. A4 covers first-time use and sustained operation. Use measurements to guide architectural changes, with the existing synchronous and threaded implementations as the initial control.

## Experiments suggested by the legacy source

These hypotheses came from earlier source inspection and still require reproduction. File names refer to the former project; use the current implementation for new experiments. See [source provenance](source-provenance.md) for the snapshot scope.

| ID | Source observation | Experiment and candidate adjustment |
|---|---|---|
| T-01 Processes and RPC | `transport.py` polls for request responses, uses a pipe for stderr, and reads stdout in a loop | Measure first startup, idle CPU, and response latency. Inject large stderr output, EOF, truncated JSON, and late replies. Evaluate event wakeups, bounded draining, and explicit shutdown from the results. |
| T-02 Events and results | Grok session chunk/tool lists keep growing; Codex recent_events is capped at 200, with messages and turns stored separately | Test long output and many tool events. Preserve existing truncation and evaluate byte-budgeted windows, artifact files, cursor pagination, and recovery for late readers. |
| T-03 Archiving and recovery | Terminal states already trigger archiving; idempotency sets and full JSONL reads exist | Preserve automatic terminal-state archiving. Measure startup scans, transactional writes, and result projections. Check repeated full scans on normal query paths. |
| T-04 Concurrency and queues | Claude limits process count; the legacy shared layer lacks a durable task queue and billing-pool scheduling | Measure queuing, fairness, cancellation, and overload rejection. Evaluate bounded queues and account/workspace constraints. |
| T-05 Completion decisions | Output can carry `NodeResult`; full acceptance also requires other evidence | Fix a set of failing tasks and incorrect self-reports. Measure false completion, missed review, and repair convergence. Check quality gates before comparing speed. |
| T-06 Configuration and connections | Native processes are initialized in several places; configuration includes author-specific paths and default tool locations | Measure discovery, authentication checks, process startup, and the first model event separately. Test connection reuse only after account/host/configuration fingerprints match and isolation checks pass. |

Treat connection reuse as a separate experiment. Its scope must satisfy account, workspace, and permission isolation requirements. Native backend capabilities determine whether one session supports parallel turns. Check approval cross-talk, cancellation interference, and resource release on exit.

## Observation records

For each experiment, record the Asterun commit, configuration revision, Python and SQLite engine versions, dependency-lock hash, OS/CPU/memory, disk type, actual backend/model identifiers, native CLI version, task set and input hashes, concurrency settings, budget, and cold-start status. Use references without secrets for accounts and billing pools. Keep tokens, cookies, and full authentication files in their existing credential stores.

Correlate events with `task_id`, `run_id`, `operation_id`, backend session/turn references, and a sequence number within each run. Record UTC timestamps across processes and use a monotonic clock for durations within a process. Calibrate host clocks before calculating cross-host latency.

Instrument request receipt, admission completion, intent commit, queue entry, backend startup, request dispatch, first public event, turn end, evidence persistence, acceptance completion, cancellation request, confirmed termination, and recovery completion. Report runner time, model inference, and human waiting separately.

Observations remain in the user's runtime environment by default. Diagnostic exports are user-selected and redacted before export. Ordinary performance logs contain state, hashes, counts, error categories, and required references. Manage source code, prompts, and raw tool input as task data. Store public model output under the task-data policy and exclude hidden reasoning from diagnostic material.

## Metric definitions

| Metric | Definition | Decision supported |
|---|---|---|
| Time to first accepted task | Start of configuration to the first task passing acceptance; split manual login, downloads, model time, and runner time | Setup clarity and repeated work |
| Admission latency | Request receipt to intent commit and return of references | Whether long tasks block CLI/MCP requests |
| Queue time | Admission completion to allocation of an execution slot | Concurrency, account-pool, or reviewer bottlenecks |
| First-event latency | Backend request dispatch to the first public event | Startup overhead and backend waiting |
| Runner overhead | Direct fake-scenario measurements of admission, scheduling, normalization, persistence, and result reads | Actual cost of each core stage |
| Terminal-state visibility latency | Receipt of the backend terminal state to durable readability of that state and key evidence | Stale displays and results lost on exit |
| Cancellation confirmation time | User request to demonstrable backend termination; report acknowledgment time and unconfirmed cases separately | When cancellation actually completes |
| Recovery reconciliation outcomes | Separate counts for established results, cases needing human checks, and unrecoverable cases | Correct pauses and erroneous redispatches |
| Resource use | Separate runner and subprocess RSS/CPU; also record open handles, queues, event buffers, and DB/WAL/artifact bytes | Leaks and growth under long output |
| Acceptance pass rate | Tasks passing the agreed quality gate / all valid attempts in a fixed task set; retain failures, timeouts, and cancellations | Completion quality on the same task set |
| Repair and human intervention | Repair rounds, re-reviews, human decisions, and handoff reasons per task | Rework and operator effort |
| Resource cost | Each provider's native tokens, request counts, subscription windows, charges, and unknowns; aggregate comparable units | Consumption in its reported scope |

Measure runner overhead directly with fake scenarios and report live model duration separately. Preserve unknown cost values; monetary conversions need verifiable data.

## Experiment layers and sampling

| Layer | Fixed scenarios | Initial sampling and reporting |
|---|---|---|
| B0 Pure logic | Configuration parsing, authorization matching, schema validation, state transitions, revision conflicts | Fixed inputs and random seeds. Prioritize correctness; keep microsecond differences in benchmark reports. |
| B1 Fake end-to-end | Short tasks, long output, approval waits, two entry points, slow consumers, duplicate requests | Warm up 20 times per scenario, then collect at least 100 samples. Test concurrency at 1/2/4/8. |
| B2 Fault injection | Before/after intent commit, after sending but before reply, before/after terminal-state persistence, process crashes, full disks, cancellation races | At least 10 fixed seeds per failure point. Report each outcome category and duplicate side effects. |
| B3 Sustained operation | 1,000 sequential fake tasks, plus at least 30 minutes of mixed event load | Record time series, resource release, queues, and checkpoints. Report total event count and bytes. |
| B4 Live backends | Text, read-only file summaries, continuing sessions, interrupted recovery, cancellation/approval capabilities | Calibrate with one example, then expand within the authorized budget. Record all failures and unsupported cases. |
| B5 Workflow quality | Fixed small-repository tasks and separate non-code tasks, comparing backend combinations | Start with 6–10 tasks, aiming for 3 runs per condition. For small samples, report individual results and medians; assess statistical significance after enough samples are available. |
| B6 User experience | First task in an empty environment, skipping optional backends, expired login, second task, human takeover | Cover at least another user/independent account environment and retain records of an unfamiliar user's actual interaction. |

Ordinary CI uses offline tests, temporary directories, and fake backends, with correctness determining the result. Trigger sustained-load and timing benchmarks separately and record the environment. Enable live backends separately and limit task counts to the authorization. Until a budget is confirmed, prepare the experiment plan and example list. Experimental concurrency and production defaults are maintained separately.

The native compatibility matrix records “OS + Asterun + adapter + CLI/App Server version + account method + execution host.” Report installation, authentication, tasks, continuation, cancellation, approvals, events, Desktop visibility, and manual continuation separately, each with `passed / failed / not_tested / not_supported` and evidence. Validate Linux headless operation and macOS Desktop presentation independently.

## Candidate performance targets and correctness requirements

Use a separate development machine with 2 vCPUs, 8 GiB memory, and a local persistent disk as the initial reference environment. Minimum hardware requirements need separate confirmation. Revise candidate targets after the first A1 baseline and retain the rationale. Compare absolute latency within the same environment.

| Item | Initial candidate target | Measurement conditions |
|---|---|---|
| Fake admission latency | p95 ≤ 250 ms | Durable writes enabled, single-host concurrency 4; exclude installation, startup, and model time |
| First page of state and events | p95 ≤ 100 ms | Bounded page size, 10,000 historical run records; exclude remote probes |
| Durable terminal-state visibility | p95 ≤ 1 s | Start when the backend terminal state enters the adapter; persistence completes in the background, with readability observed directly |
| Additional runner RSS | Idle ≤ 150 MiB; ≤ 300 MiB with 8 active fake runs | Count native backend subprocesses separately and report long-output cases separately |
| Resource recovery after sustained load | After warmup stabilizes, handles/threads/memory show no linear growth with completed tasks; return to the stable range at the end | Disk artifacts grow according to retention policy, with normal logging maintained |
| Tuning benefit | Target metric improves ≥ 15% in the same environment, confirmed in at least 3 repeat rounds with no quality regression | Suggested basis for retaining performance changes; assess valuable correctness fixes separately |

Authorization bypass, known duplicate side effects, acceptance against the wrong version, loss of key terminal states, and false cancellation reports are failures. In B2, pausing with evidence when a remote outcome cannot be established can pass the boundary check; the business result remains unconfirmed. Unsupported redispatch or success reporting counts as a failure.

Report individual results for small live samples; calculate tail latency once there are enough samples. Mark unmeasured fields `not_measured`.

## Candidate runtime settings

This table retains early A3 proposals for experimental comparison. See the [installation guide](install.md) for current defaults. After validation, record adopted changes in configuration and documentation within the user's authorized scope.

| Setting | Starting proposal | Conditions |
|---|---|---|
| Global active runs | 2 | Also satisfy backend, account-pool, and workspace-write constraints |
| Active runs per account/billing pool | 1 | Preserve original quotas separately while sharing relationships are unknown; record the source of user-declared relationships |
| Queue limit | 32 | Return busy/retryable when full and keep thread creation bounded |
| Writers per workspace | 1 | Check input versions for read-only tasks too; worktrees separate files, with OS isolation configured separately |
| Automatic repair rounds | 2 | Revalidate the target version each round and return control to the user at the limit |
| Safe read-only request retries | At most 2, with backoff and random jitter | Stop retries after authentication failure; handle rate limits according to the backend response |
| Event query page | 100 records by default, plus a total byte limit | Put long output in artifacts and provide a reconciliation path when a cursor expires |
| Cancellation confirmation wait | Initial observation window of 15 s | Mark timeout as pending confirmation/reconciliation; completion requires a termination receipt |

Time handshakes, RPC responses, task execution, queuing, and user-input waits separately. If an experiment releases execution slots during user-input waits, also record the backend session's resource use. Manage task lifetime independently of individual RPC timeouts. Invalidate connection caches by account, host, permissions, and configuration fingerprint.

## Quality and backend-combination experiments

Select implementation and review backends through configuration. Compare the same input revision, tool permissions, tests, acceptance rules, and repair limit.

Candidate P1 is “connected Grok implementation + independent review.” P2 is “another stronger model implementation + the same independent-review requirements.” Record “direct implementation by a strong model” separately as P3 and assess it at its own assurance level. Report the cost saved by omitting review separately from gains achieved at equivalent quality. Verify actual model IDs, reasoning settings, and availability before running.

Include mechanically checkable functional fixes, edge cases, dependency/API changes, clean-review examples, and non-Git document tasks. Fix the inputs and keep acceptance material separate. Withhold reserved evaluation answers from the implementation model. Evaluate code review by known-defect detection and false positives.

Record end-to-end completion time, waits at each stage, all failures and retries, resource consumption, pass rate, introduced regressions, repair rounds, and human intervention. List additional independent-judge calls and costs separately for every group. Rank costs when the data is comparable; otherwise report original units.

Before changing default routing, confirm unchanged quality requirements, no expansion of permissions or budget, and stable benefits across different tasks. Include experiment records, applicable scope, failure fallback, and configuration revision with the change. Credit purchases and changes to paid API billing require separate authorization. Experiments stay within confirmed tasks and budgets.

## Experiment artifacts and maintenance

Store raw records in a local runtime directory ignored by Git. Keep redacted experiment configurations, examples, and summaries in the repository. Each report includes at least `experiment_id`, baseline/candidate commits, task-set hash, environment, sample size, raw-record references, result distributions, failure reasons, quality conclusions, and the adoption decision.

Change one main factor per round, such as event buffering, polling strategy, transaction batches, connection reuse, or concurrency. Start with a small comparison. Revert the candidate configuration or code immediately if correctness regresses. Verify that a consistent backup can be restored before a database upgrade, and use compatible databases with older binaries.

Use a consistency-preserving mechanism for SQLite online backups and manage Asterun's artifact manifest and hashes alongside it. A running database also has state such as its WAL; use the [SQLite backup interface](https://sqlite.org/backup.html) for a complete database backup.

A4 adds retention and cleanup rules: retain operations awaiting reconciliation and audit evidence, and check active references before cleanup. Limit resets to Asterun-owned state while preserving users' native threads, code, and shared accounts. Revisit performance and failure distributions after every upgrade, expanding regression coverage when new changes or failure evidence warrant it.

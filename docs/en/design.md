# Asterun design

[中文](../design.md) | [English](design.md)

Asterun runs agent tasks on a user's computer or server. It manages configuration, task state, native sessions, permissions, scheduling, and result evidence. The CLI, MCP clients, and applications such as GrokBot use the same application service. The core also works on its own.

This document describes `0.1.0a13`. The first release's feature scope is frozen. See the [release notes](release-v0.1.md) for backend support and live validation, and the [protocol implementation](protocol/IMPLEMENTATION.md) and [capability profile](capability-profile.md) for interface details.

## Responsibilities

| Component | Responsibility |
|---|---|
| Entry adapters | Accept tasks, establish the caller's identity, present state, and forward approvals and results |
| Core | Validate configuration and authorization, persist task and session bindings, schedule work, retain evidence, and reconcile recovery |
| Backend adapters | Use native protocols for threads, turns, events, approvals, and terminal results |
| Optional plugins | Provide declared capabilities from separate installations, with explicit account and billing pool bindings |
| Optional workflows | Run checks, independent reviews, repairs, and verification against a fixed input version |

The Python core gives a resident executor ownership of execution on one host. The CLI, stdio MCP, and same-user Unix socket share application logic. Configuration, sessions, policy, storage, and evidence have separate modules. Asterun owns its interfaces and releases; callers own their application accounts, templates, and message delivery.

## Execution hosts and accounts

Users authenticate each backend and configure workspaces on the host where that backend runs. GrokBot may call Asterun inside its cloud computer or invoke a local Asterun process through an existing authorized channel. Caller location, execution host, account, and native session are recorded separately. The calling channel supplies any cross-host transport.

`model_provider` identifies the model source, while `execution_backend` identifies its execution interface. `account_ref`, `runtime_ref`, and `billing_pool_ref` bind the account, host, and billing pool. Interfaces that share an account or pool retain that resource relationship. Native tools or dedicated secret stores hold credentials; the core uses references.

Installation, sign-in, authorization, actual invocation, native resume, and client visibility have separate verification states. Capability declarations describe adapter coverage. Live records identify what was tested with a particular version and account environment.

## Tasks, sessions, and permissions

`conversation_id` links the conversation, `task_id` identifies the task, and each attempt has its own `run_id`. `backend_session_id` and `backend_turn_id` bind native references to an account, host, and workspace. A retry preserves the task relationship. Reusing a native thread depends on adapter support and binding checks.

The entry point establishes the caller's identity. Before execution, the core checks the workspace, action, authorization expiry, configuration revision, and backend capability. Role policy defines the allowed scope; native permission modes control backend execution; operating-system isolation is configured separately. Tests, builds, and dependency installation count as code execution. Valid authorization can be reused within its scope.

Codex retains native App Server threads and supports fixed-snapshot reads and optional desktop project registration. Grok's autonomous coding mode operates within configured workspace and command scopes. See the [user manual](user-manual.md) and backend guides for resume, approval, and cancellation conditions.

## State, idempotency, and recovery

SQLite is the durable source for task and control records; the current database version is schema 6. Events, artifacts, and exported reports refer to those records. External calls follow validation, transactionally recorded intent, invocation, and result persistence. Database transactions are released while waiting for a backend.

Each interface defines the identity, method, and target scope of its idempotency keys. Retrying a key reads the original operation; changing the input produces a conflict. If a request may have arrived but its result is unknown, the core reconciles using native references. An unresolved result retains its unknown state and associated budget reservation.

Execution, acceptance, and delivery are recorded separately. Cancellation first records intent, then uses backend evidence to confirm termination. Human takeover stops further orchestration. Resuming requires fresh checks of the latest turn, input version, pending approvals, and target changes. Activity in an external native client requires its own verification.

A consistent snapshot is created before a database upgrade. Rollback uses a snapshot copy compatible with the older release and retains the newer database for reconciliation. See [installation](install.md) for backup coverage and recovery steps.

## Workflows and resources

A task defines its objective, resources, allowed actions, output, checks, and budget, with one `orchestration_owner`. When the caller orchestrates, the core executes submitted steps. When the core orchestrates, the caller submits the objective, answers input requests, and observes results.

Code quality workflows run checks, independent review, repairs, and verification against a fixed revision. Findings bind to a commit or file hash. Reaching a round or budget limit hands back the current result. Gate verdicts, check evidence, and publication state are returned separately. See [PR review](pr-review.md).

Optional plugins use fixed execution plans. Submission rechecks the input, configuration, installed plugin, account, permissions, and shared pool, then records reservations and dispatch intent in one transaction. Usage retains its original units, source, and unknown values. First-release routing is explicitly configured. See [plugins](plugins.md).

Observers read results within bounded windows. The host schedules subsequent observation and chat delivery. Inbox consumers acknowledge results after the host confirms delivery. See [result delivery](review-consumer.md).

## Maintenance and verification

Default checks use temporary directories and fake backends. Live backend behavior, billing, native client visibility, and human takeover are tested separately. Records identify the exact commit, environment, commands, and unverified items.

See the [development plan](development-plan.md) for current maintenance and the [tuning plan](tuning-plan.md) for proposed experiments. Earlier decisions remain in the [ADRs](../adr/) (Chinese reference). See [source provenance](source-provenance.md) for the current snapshot. Historical decisions retain their original context; use this document and the operating guides for current behavior.

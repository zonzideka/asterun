# Optional plugins

[中文](../plugins.md) | [English](plugins.md)

Install a plugin in a separate environment, then register it with Asterun. Built-in backends continue to support v1 configuration; external plugins use v2. The Antigravity account, Cursor local Agent, and Jules REST packages are previews. Live provider calls and billing still need validation.

## Configuration and migration

In v2, `plugins` stores installation registrations, `provider_accounts` holds provider identity references, `connections` pins authentication, environment, and model, `billing_pools` describes shared resource pools, and `backends` aliases point to connections. The optional `quota` section configures the Codex quota probe, passive collection, and the Grok quota probe, which is off by default; fields and defaults are below. The optional `context_budget` section configures context-capsule and segmentation budgets. `enforce` is token-budget admission (see the ledger notes in the next PR); segmentation is not implemented yet. Omitting the key leaves the configuration digest unchanged. Fields are below. Start with the [offline fake example](../../examples/plugins/fake-v2.json).

Inspect a migration candidate for a v1 configuration:

```sh
asterun --config old.json config-migrate
```

After reviewing the candidate and `source_sha256`, write a new configuration and backup:

```sh
asterun --config old.json config-migrate \
  --output new.json --backup old.backup.json \
  --expected-source-sha256 REVIEWED_SOURCE_SHA256
```

Use separate paths for the source, output, and backup. The output and backup must not exist. Migration preserves the original configuration, writes the backup with mode 0600, and increments the revision. Real connections are initially disabled in the migrated configuration; verify account, environment, and billing settings before enabling them. Fake connections retain their enabled state. Existing tasks and sessions retain their bindings.

Built-in compatibility plugins are `asterun.fake`, `openai.codex`, `xai.grok`, `anthropic.claude`, and `google.antigravity-cli`. They reuse the original adapters and execution switches.

## Install and register

External plugin entries specify `manifest_path`, runner argv, `installation_path`, and SHA-256 values. The runner may only be an absolute interpreter followed by `-m` and a module name, or a single absolute executable path. `-c`, any other interpreter option, a relative path, and an interpreter-plus-script pair are rejected at registration. Registration must also supply `runtime_path` and `runtime_sha256`, and the pin must be the copy that will be executed or imported. For `-m package.worker`, `runtime_path` must cover that module file and its package directory: the package directory itself, or the installation tree that contains the package. A single script is pinned as that script or its parent directory. The entry is resolved only inside this pinned tree. Registration does not start the interpreter to discover the import path, so `site`, `.pth`, and `sitecustomize` do not run. A `-m` launch uses `-I -S -B` and inserts only the pinned import root onto `sys.path`. Pinning only the interpreter, the wheel file, or another copy that this run will not import is rejected. An unpinned plugin is rejected by default. `allow_unpinned_runtime: true` is an explicit development switch, off by default; it only allows the configuration to load, and execution still refuses a plugin without a runtime digest. Before execution, the digest of that copy is recomputed and the entry must still lie inside it. The wheel's `installation_sha256` pins the distribution artifact and does not replace the runtime pin. Use a separate venv. After changing the installed version, register it again and prepare new plans. Plugins run with the current OS user's permissions, so use trusted code.

`plugin-register --request registration.json` accepts `plugin_id`, the complete `registration`, `output`, `backup`, and `expected_source_sha256`. It backs up the original configuration and writes a candidate. Review it, restart the core with the candidate, and apply its revision as described in [installation and configuration](install.md).

Package setup is documented for [Antigravity](../../packages/asterun-plugin-antigravity/README.en.md), [Cursor](../../packages/asterun-plugin-cursor/README.en.md), and [Jules](../../packages/asterun-plugin-jules/README.en.md).

## Use and disable plugins

`plugin-list`, `plugin-inspect PLUGIN_ID`, and `connection-setup CONNECTION_REF` provide static checks and setup guidance. `plugin-disable` writes a persistent instance record that blocks new actions. `plugin-enable` clears that record for the same installation identity; the plugin must also be enabled in configuration.

Agents use `task-submit`. Structured tool capabilities use `capability-invoke --request invoke.json`:

```json
{
  "workspace": "demo",
  "backend": "external",
  "capability": "local.echo",
  "input": {"text": "hello"},
  "idempotency_key": "demo-1"
}
```

Inputs and outputs are validated against the manifest Schema and stored in `Task.capability_input` and `Run.output`. For a reviewable fixed plan or a code-quality workflow, use the [capability profile](capability-profile.md).

CLI and MCP share request definitions; MCP tool names use underscores. `usage-inspect` shows the local budget ledger and provider observations already received. `quota-inspect` (MCP `quota_inspect`) reads the local quota cache and does not query upstream. `quota-set` records a manual entry at low confidence for at most 24 hours. `quota-clear` removes only manual entries. `quota-refresh` (MCP `quota_refresh`) only reads the Codex app-server method `account/rateLimits/read`, keeps a default minimum interval of 60 seconds, does not start a thread or consume quota, and does not store balances or email addresses. When a Codex run receives `account/rateLimits/updated`, the run writes that snapshot into the quota cache at completion with source `passive_codex`, and does not store balance, email, or plan. The v2 `quota` section controls the probe and this passive collection: `probes.codex_app_server.enabled` defaults to true, `ttl_seconds` defaults to 600 (60–3600), `min_interval_seconds` defaults to 60 (60–3600), and `timeout_seconds` defaults to 10 (1–30); `passive.codex_rate_limits_updated` defaults to true. Unknown fields are rejected. `routing-explain` (MCP `routing_explain`) only explains candidate-group selection and does not change dispatch; see below. After disabling a plugin, the original principal can still read, cancel, or reconcile existing tasks under their original bindings, subject to the plugin's capabilities.

Grok quota reads use the undocumented ACP extension `_x.ai/billing` and are off by default. Enable them in v2 by setting both `enabled` and `acknowledge_undocumented` on `quota.probes.grok_acp_billing` (both default to false). The probe only calls `initialize` and `_x.ai/billing`. It does not create a session or send a prompt. Confidence is at most medium. It records the used ratio, the billing-period end, and whether on-demand billing is enabled, and it does not store amounts. On-demand state is read only from the boolean field `onDemandEnabled`. Amounts, caps, used values, and unknown fields are ignored. `onDemandEnabled` set to false stays off even when a cap is greater than zero. `billingPeriodEnd` may be absent; if it is present but expired or invalid, parsing fails and produces no observation. `ttl_seconds` defaults to 900 (60–3600), `min_interval_seconds` defaults to 300 (60–3600), and `timeout_seconds` defaults to 10 (1–30). v1 configuration cannot enable this probe. The probe returns only three error codes: `AUTH_REQUIRED` (authentication or login failure), `PROBE_TIMEOUT` (call timeout), and `SCHEMA_MISMATCH` (missing binary, closed connection, process failure, or a response that does not match the billing shape, including an expired or invalid billing-period end). It does not return `LIVE_DISABLED` or `PROBE_FAILED`. When `ASTERUN_RUN_GROK=1` is unset, the service records `skipped=disabled` and does not call the probe. When the two switches are not both on, the service records `PROBE_DISABLED`. Neither disabled state is a probe error code.

A persistent quota latch comes from a saved native failure classification and does not keep the raw error text. A Grok HTTP 402 quota exhaustion stops later dispatches in the same billing pool. Antigravity latches only on a strict 429 quota exhaustion: the text contains an independent `429` token (`\b429\b`) or `RESOURCE_EXHAUSTED`, and it contains quota (case-insensitive). A plain rate limit does not latch. Antigravity isolates quota buckets, with Gemini in one group and Claude and GPT in the other. Clear a latch by changing that backend's `quota_epoch` on v1, or by updating the billing window on v2. `quota-inspect` shows a window with freshness `latched` at the front of the matching scope. That window is display-only and is not written into the quota cache.

## Context budget (`context_budget`)

v2 may include the optional `context_budget` namespace for context-capsule and segmentation budgets. It shares no fields with billing, `routing`, or `quota`, and it does not change validation of those three. When the key is omitted, read-back does not fill defaults, and `to_dict` and the configuration digest stay byte-for-byte the same. The key appears in serialization only when it was written. Capsule, segmentation, and assignment fields that were written are filled with the defaults below. A threshold omitted inside `profiles` stays omitted, which means that threshold is unset. v1 has no such section; readers get the `mode=off` defaults.

`mode` accepts `off` (the default), `observe`, and `enforce`. `enforce`: token-budget admission (see the ledger notes in the next PR); segmentation is not implemented yet. When `mode` is `enforce`, at least one profile must include `task_hard`, or the configuration is rejected. Any other value is rejected. `token_basis` accepts only `processed` (the default).

`capsule.nonmandatory_soft_bytes` is 1024–65536, default 16384. `capsule.nonmandatory_hard_bytes` is 1024–131072, default 24576, and must be greater than or equal to soft. `capsule.notes_max_bytes` is 0–16384, default 4096. `capsule.progress_markers` is a boolean, default true.

`segmentation.backends` is a list of configured backend names, default `[]`. `segmentation.roles` is a subset of `implementation` and `repair`, and the default is both. `segmentation.min_long_task_processed` is 0–1000000000, default 600000. `segmentation.resume_max_residency` is 0–100000000, default 100000. `segmentation.observe_capsule_min_processed` is 0–1000000000, default 600000.

`ab_assignment.enabled` is a boolean, default false. `ab_assignment.enforce_ratio_bp` is 0–10000, default 5000. This release does not enforce; the ratio is stored only.

`profiles` keys use the form `backend/role`. The backend must already appear in `backends`. The role must be `implementation`, `repair`, `review`, or `planning`. Every field may be omitted: `segment_turns` is 1–1000; `segment_soft`, `residency_soft`, `task_soft`, and `task_hard` are each 1–10000000000; `max_segments` is 1–100; `segment` is a boolean. Thresholds that are present must satisfy `task_hard ≥ task_soft ≥ segment_soft`. A `review` profile cannot set `segment=true`.

Unknown fields are rejected. A field named `enforcement`, and the value `hard`, are always rejected. A boolean is not an integer. Every number must be an integer.

Observe and enforce both record, when a run reaches a terminal status, that run's processed/new counts and the task's cumulative processed/new counts, plus the flags that would have fired (`segment_soft_exceeded`, `task_soft_exceeded`, `task_hard_exceeded`, `max_segments_exceeded`, `long_task`, `would_segment`, `capsule_candidate`). A capsule is also written when `capsule_candidate` is true. Read them with `asterun context-budget-inspect` or the `context_budget` field of task-get. This does not change dispatch, returned results, or counters, and it does not segment. When mode is `off`, this record does not read or write the database.

## Context capsules (v1, observe only generates and does not dispatch)

When `capsule_candidate` is true, a continuation capsule is written after the observation row, with `trigger` `observe_only`. `capsule.progress_markers` does not affect this step, whether it is true or false. The capsule is stored only: `ready` is 0, nothing is dispatched, and the run result and counters stay unchanged. If generation fails, only that capsule is dropped; the observation row and `save_run` still complete. When `mode` is `off`, this step does not read or write the database.

The capsule schema is `asterun.capsule/v1`. It keeps the contract, a workspace summary, progress, and open items needed to continue. Mandatory text is never trimmed: `objective` is a byte-for-byte copy of the task text, and `constraints`, `acceptance`, `review_requirements`, `findings` (including id and text), and `limits` are copied the same way. Non-mandatory content has a soft limit of 16384 bytes (16 KB) and a hard limit of 24576 bytes (24 KB). Past the soft limit, lossy trimming runs in a fixed order (note text, commands, files touched). If the hard limit still does not hold, the body is not stored and the row's `error_code` is `CAPSULE_TOO_LARGE`.

A capsule does not contain source text, diff text, chat, or credentials. The workspace keeps HEAD, branch, dirty paths and statuses, file sha256 values, and the file/insertion/deletion counts from `diff --shortstat`. Each command item stores only `program` (the basename of the first token, or null when it does not match the rule), plus the `sha256` and `length` of the full command. Rendering shows only the program name and the first 12 characters of the digest. Arguments and the command body are not stored. An unconfirmed tool call keeps the tool name and an input digest.

Lines marked `ASTERUN-NOTE:` become low-trust notes (`trust=low`). They do not change any task constraint. Note text is at most 4096 bytes. The rendered prompt labels them as low-trust notes that do not change task constraints. Whitelist filtering discards any line that cannot be confirmed safe, and counts it only in `dropped_lines`.

Verification is redone on read and does not trust a previous conclusion stored in the row. An empty list means the capsule checks out. Problem codes are `schema_mismatch`, `body_sha256_mismatch`, `capsule_id_mismatch`, `mandatory_sha256_mismatch`, `mandatory_mismatch`, `finding_missing:<id>`, and `finding_text_changed:<id>`. If the task contract digest no longer matches the one stored in the capsule, `contract_changed` is added.

One row is kept per `(namespace, run, trigger)`; generating again updates that row. Table `context_capsules` is created on older databases automatically, and the schema version does not change. Rows are kept for 30 days and at most 2000 rows. Pruning deletes rows only, not blobs. A backup that lacks this table still restores. A backup that has the table with different columns is rejected.

Read it with:

```sh
asterun capsule-inspect --task TASK_ID [--limit N] [--render]
```

The MCP tool name is `capsule_inspect`. `task_id` is required, `limit` is 1–200 and defaults to 20, and `render` defaults to false. Permission matches `context-budget-inspect`, and the caller also needs `task.get` on that task's workspace. The command does not create the table, write the database, or dispatch. When the table is missing, `capsules` is an empty list. `--render` loads the newest capsule for that task that has a body, verifies it against the current task, and returns `verify_issues` and `rendered_prompt`. The task contract inside the prompt is the original text stored in the capsule.

## Candidate groups and routing explanation

Candidate groups exist only in configuration v2, under `routing.candidate_groups`. A group is 1 to 8 aliases with the same role, the same backend kind, and the same execution options. Models may differ. `models_allowed` is optional and is checked only when written. `primary` must be one of the members. A group name matches `^[a-z][a-z0-9_-]{0,31}$`. Each role has at most one group, and members of different roles cannot overlap. When `candidate_groups` or `quota_preference` is omitted, read-back does not fill defaults.

Role and kind: `implementation` is grok only, `review` is codex only, and `planning` is antigravity or claude. Every member of one group must share a kind. Execution-option comparison ignores the backend fields `name`, `home`, `bin`, `model`, `connection_ref`, `quota_epoch`, and `extra_env`, and the connection option keys `bin`, `home`, and `model`. Each member's billing pool must be `subscription` or `local`, and must not set `paid_overage_allowed`. A review group cannot include an implementer's backend, provider account, or a model from the same vendor: kind grok is xai; kind codex, or a model starting with gpt or with o followed by a digit, is openai; a model starting with claude, or kind claude, is anthropic; a model starting with gemini is google.

`routing.quota_preference.mode` defaults to `off`, which behaves exactly as before. `enabled` set to false is the same as `off`. `dry_run` still dispatches primary and only records `would_choose`; `on` dispatches the chosen candidate. See "Quota preference (dry-run, then enable)" below. Defaults are a 72-hour horizon, a 1000 bp waste threshold, a 500 bp switch margin, confidence of at least medium, short windows excluded from value, a 500 bp short-window floor, and a 1000 bp long-window floor. `count_reset_grants` can only be false. Paid overage and reset grants do not count as available quota or as waste.

Scoring reads the local quota cache. Exhausted, latched, or fully used (10000 bp) candidates are excluded. Remaining quota below the floor plus one run's estimate drops to tier 3. Only windows that reset within the horizon and last at least 24 hours count toward waste; a 5-hour window only gates unless short windows are enabled. In-flight runs deduct 100 bp on a weekly window and 500 bp on a 5-hour window. No fresh window means unknown. The choice falls back to primary when every member is unknown, the primary is unknown, or every member is latched. A suggestion leaves primary only with fresh evidence that beats the switch margin.

`routing-explain --group NAME` or `routing-explain --backend @NAME` (MCP `routing_explain`) runs that read-only calculation and returns the suggestion with `dispatch_changed: false` and `preference_enabled`. Permission matches `quota-inspect`. v1 configuration, or v2 with no candidate groups, returns `INVALID_CONFIG`. This command only explains; it does not dispatch. Whether a task dispatches the chosen candidate depends on `mode`.

```json
{
  "routing": {
    "candidate_groups": {
      "implementer": {"role": "implementation", "members": ["grok-a", "grok-b"], "primary": "grok-a"},
      "reviewer": {"role": "review", "members": ["codex-a"], "primary": "codex-a"},
      "planning": {"role": "planning", "members": ["agy-opus", "agy-gemini"], "primary": "agy-opus"}
    },
    "quota_preference": {"enabled": false}
  }
}
```

`agy-opus` and `agy-gemini` are both antigravity, with the same execution options. Their models are Claude and Gemini.

### Quota preference (dry-run, then enable)

`routing.quota_preference.mode` is `off`, `dry_run`, or `on`. `off` is the default and behaves exactly as before. `enabled` set to false is the same as `off`. `dry_run` is a rehearsal: dispatch stays on primary, and the result is only recorded as `would_choose`. `on` dispatches the chosen candidate.

`on` is accepted only when `automatic_account_switch` is true, the candidate groups are non-empty and all pass validation, and `rules` is empty. `automatic_paid_fallback` is always false. Setting `automatic_account_switch` to true while `mode` is not `on` is rejected.

The entry is `backend` set to `@group`, or `backend` omitted and `default_backend` set to `@group`. An explicit ordinary backend name does not enter preference. A missing group returns `INVALID_REQUEST`. On v1, `@…` returns `INVALID_CONFIG`. `workflow.review_backend` does not accept a candidate-group alias in this version; quality review still uses a fixed backend. `default_backend` may be `@group` (v2 only).

Paid protection for an explicit backend: if a fresh window at confidence of at least medium shows the subscription quota is exhausted, and that same scope has a paid-overage entry, the call is blocked with `BUDGET_EXHAUSTED` by default. Set `routing.quota_preference.allow_paid_on_explicit` to true to turn this off; the default is false. Unknown or stale data does not block.

Resume and repair rounds keep the backend selected the first time. If the chosen candidate fails admission, the call errors and does not try the next one. When quota data is invalid, dispatch falls back to primary with `reason_code` `quota_state_invalid`.

Decisions go to `plugin_routing_decisions`: at most 1000 rows, kept for 30 days, score summaries only. Credentials and balances are not stored. When the entry above was used, `task-get` shows task-level `routing` with `mode`, `group`, `chosen`, `primary`, `reason_code`, and `would_choose`. Otherwise that key is omitted.

Watch `routing.would_choose` under `dry_run` before setting `mode` to `on`. Paid overage is never used automatically, and reset grants are never redeemed automatically.

## Credentials and recovery

Configuration uses credential references: `env:NAME`, `file:/absolute/path`, `native-home:/absolute/path`, or `credential-ref:REFERENCE`. Environment and file credentials must match the manifest's authentication declaration. A credential file must be a private, single-line regular file owned by the current user, with a verifiable path. Native accounts use an explicit private HOME. Deployments provide resolvers for opaque references.

The worker starts with a fixed PATH, temporary HOME/TMPDIR, and credentials injected for the authorized connection. A `-m` launch also passes `-I -S -B` and does not load `site`. Each call rechecks the runner, manifest, wheel, and the runtime copy that will actually be executed. Protocol frames, output, and runtime have limits. Secrets enter the in-memory environment at startup; manage and back up native HOME contents separately.

After an execution request has been sent, a disconnect, timeout, or malformed response preserves `pending_reconcile` and an uncertain budget reservation. Reconcile the original task; cancellation depends on the remote terminal state. Plugin events are recorded as provider observations, and usage reports retain their `provider_observed` source.

V2 records bind the reading principal, connection, and runtime identity. Retain core state, configuration, native HOME, and plugin-private state for recovery. V1 history retains its original authorization rules. Database downgrades use a compatible snapshot copy while preserving newer run records.

# Fixed capability plans

[中文](../capability-profile.md) | [English](capability-profile.md)

With `asterun-capability/v1`, discover capabilities, prepare a fixed plan, review and submit it, then query the result. This entry point requires v2 configuration and SQLite. CLI commands use hyphens, such as `capability-discovery`; MCP names use underscores. Pass prepare, submit, and get requests in a `--request` file.

## Prepare and submit

Preparation checks local registration, input Schema, permissions, installation digests, configuration, billing policy, and the code baseline. The returned plan is valid for 15 minutes and lists the input, connection/account references, model, authentication, plugin version, budget estimate, and digest. Execution and budget reservation begin at submission.

A general call uses a backend and capability from the current registry:

```json
{
  "schema_version": "asterun-capability/v1",
  "namespace_id": "ns_local",
  "workspace": "demo",
  "backend": "external",
  "capability": "local.echo",
  "input": {"text": "offline", "mode": "success"},
  "workflow_id": "wf_capability_call",
  "idempotency_key": "prepare-example-0001"
}
```

After reviewing the plan, submit its `plan_id`, `plan_sha256`, `schema_version`, `namespace_id`, and a new idempotency key. The account, model, and input remain fixed by the plan. Each plan corresponds to one execution; same-key retries read the original receipt.

The submission transaction saves the standard Task/Run, assignment, authorization, reservation, and outbox, then returns `ACCEPTED`. The persistent core advances the queue. A local client can also advance submitted records by reading through capability-get or standard control.

## Code workflows

Use `workflow_id=wf_code_change`, `capability=agent.execute`, and text input, with a quality section:

```json
{
  "target_paths": ["src/example.py"],
  "checks": [{"kind": "file_contains", "path": "src/example.py", "text": "def example"}],
  "max_repairs": 1
}
```

`target_paths` selects the validation snapshot; execution permissions come from the configured workspace. Preparation records file bytes, modes, and Git HEAD, then checks them again before submission and dispatch. Deterministic checks use the existing file-existence and file-content checks. QualityRuntime then performs an independent review, repairs findings, and reviews the changes again.

The plan pins the reviewer, substitute backend, repair limit, and turn budget. Implementation, review, and repair each consume one hard turn and share the original connection budget. Insufficient budget, an unavailable reviewer, an unknown remote result, or the repair limit leaves the workflow in `WAITING`, retaining references and findings.

A code workflow that passes validation produces `val_code_quality` Evidence containing the initial baseline, final snapshot, checks, independent review references, and finding closure. Standard success requires this evidence. `capability-get` also returns `quality_verdict`. Changes to files after validation produce `STALE`; the historical run retains the version it validated. General `wf_capability_call` workflows report `NOT_APPLICABLE` for quality.

## Recovery and compatibility

Standard control reads the same Task, Run, artifacts, and Evidence. Pause, cancel, and recovery follow runner-control/v1 and revision CAS. After pausing, continue observing the active native turn. Cancellation requires a native terminal result. Disabling a plugin blocks new actions; existing records remain available for reconciliation under their original bindings.

This profile retains the existing runner-control/v1 and `wf_execute@1` contracts. The flow has passed offline fake and protocol-fixture validation. Live code-quality workflows and external provider calls still need validation. See [release notes](release-v0.1.md) for backend support.

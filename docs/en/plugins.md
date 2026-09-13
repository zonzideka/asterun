# Optional plugins

[中文](../plugins.md) | [English](plugins.md)

Install a plugin in a separate environment, then register it with Asterun. Built-in backends continue to support v1 configuration; external plugins use v2. The Antigravity account, Cursor local Agent, and Jules REST packages are previews. Live provider calls and billing still need validation.

## Configuration and migration

In v2, `plugins` stores installation registrations, `provider_accounts` holds provider identity references, `connections` pins authentication, environment, and model, `billing_pools` describes shared resource pools, and `backends` aliases point to connections. Start with the [offline fake example](../../examples/plugins/fake-v2.json).

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

External plugin entries specify `manifest_path`, runner argv, `installation_path`, and SHA-256 values. `runtime_path` and `runtime_sha256` pin the installed runtime contents. Use a separate venv or an explicit non-Python runner. After changing the installed version, register it again and prepare new plans. Plugins run with the current OS user's permissions, so use trusted code.

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

CLI and MCP share request definitions; MCP tool names use underscores. `usage-inspect` shows the local budget ledger and provider observations already received. After disabling a plugin, the original principal can still read, cancel, or reconcile existing tasks under their original bindings, subject to the plugin's capabilities.

## Credentials and recovery

Configuration uses credential references: `env:NAME`, `file:/absolute/path`, `native-home:/absolute/path`, or `credential-ref:REFERENCE`. Environment and file credentials must match the manifest's authentication declaration. A credential file must be a private, single-line regular file owned by the current user, with a verifiable path. Native accounts use an explicit private HOME. Deployments provide resolvers for opaque references.

The worker starts with a fixed PATH, temporary HOME/TMPDIR, and credentials injected for the authorized connection. Each call rechecks the runner, manifest, wheel, and installation tree. Protocol frames, output, and runtime have limits. Secrets enter the in-memory environment at startup; manage and back up native HOME contents separately.

After an execution request has been sent, a disconnect, timeout, or malformed response preserves `pending_reconcile` and an uncertain budget reservation. Reconcile the original task; cancellation depends on the remote terminal state. Plugin events are recorded as provider observations, and usage reports retain their `provider_observed` source.

V2 records bind the reading principal, connection, and runtime identity. Retain core state, configuration, native HOME, and plugin-private state for recovery. V1 history retains its original authorization rules. Database downgrades use a compatible snapshot copy while preserving newer run records.

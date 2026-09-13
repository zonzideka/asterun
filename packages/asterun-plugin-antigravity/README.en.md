[中文](README.md) | [English](README.en.md)

# Antigravity account plugin

This optional Apache-2.0 plugin uses a native account through Antigravity CLI 1.2.0. Install it in a separate environment; its wheel requires only the Python standard library. Asterun loads the plugin through an explicit manifest and installation digests. Its external configuration ID is `google.antigravity-cli`.

## Build and configure

Verify the source from a complete Git checkout, then build with a prepared build environment:

```sh
python packages/asterun-plugin-antigravity/scripts/verify-source.py
python -m pip wheel --no-build-isolation --no-deps packages/asterun-plugin-antigravity
```

`vendor-source.json` pins source digests and mechanical transformations, checked against the current source. Set the runner to the absolute path of Python in the plugin environment, followed by `-m asterun_plugin_antigravity.worker`.

Configure the connection with `auth_mode=native_account` and `upstream_version=1.2.0`. Supply the native HOME through a provider_account `native-home:` reference. The options are `bin`, `binary_sha256`, `home`, `model`, and `execution_enabled=true`. `home` must match the HOME injected by the core. `timeout_seconds` defaults to 20 seconds and must be greater than 0 and at most 20. The host's overall deadline also applies.

Use the standalone entry point to prepare the HOME and bind its read scope:

```sh
asterun-antigravity-plugin prepare --home /absolute/private/native-home
asterun-antigravity-plugin bind-workspace \
  --home /absolute/private/native-home --workspace /absolute/workspace
```

See the [Antigravity guide](https://github.com/zonzideka/asterun/blob/main/docs/en/antigravity.md) for sign-in and profile setup. Execution uses a pinned binary, model, and workspace, with the prompt sent through stdin. The profile permits reads, denies writes, commands, web access, and MCP, and disables G1 credits. The native CLI continues to save its own logs and sessions.

## Results and recovery

Success requires valid initialization, complete steps, a matching terminal state, and a clean process exit. Tool errors and soft denials count as failures. Results preserve the native session ID and mark usage as `session_cumulative`.

This preview package supports a single synchronous execution. Native resume, observation, cancellation, approval, reconciliation, and quota queries across RPC calls are not yet implemented. A lost connection may leave the result `unknown`; an operator must check native state while the budget reservation remains held. When the balance is unknown, admission follows the core's bounded policy and explicit overage configuration.

The standalone wheel and protocol test doubles have been validated. Real accounts, subscription charges, official CLI execution through the external plugin, client visibility, and Linux await validation.

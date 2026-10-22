[中文](README.md) | [English](README.en.md)

# Antigravity account plugin

This optional Apache-2.0 plugin uses a native account through Antigravity CLI 1.2.0 or later. Install it in a separate environment; its wheel requires only the Python standard library. Asterun loads the plugin through an explicit manifest and installation digests. Its external configuration ID is `google.antigravity-cli`.

## Build and configure

The current source version is `1.0.9`: unattended soft denials are recognized from agy's native tool error sentence (`user denied permission for ...`) and report `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED`; Go-quoted multi-line deny sentences (for example a heredoc command) are matched strictly, and a fixed read-only preamble is prepended to the prompt sent to agy. `1.0.8`: a tool call hard-refused by a profile deny rule no longer fails the run on its own; the run succeeds when the model gives a final answer (recorded as `policy_denied_count`). An unattended soft denial now reports `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED` (recorded as `soft_denied_tools`). Shipped with core `0.1.0a28`. `1.0.7`: when an agy result is ERROR, it separates a 429 quota exhaustion from a plain rate limit with strict rules and puts only the classification (kind, status code, source, provider) into native output, never the raw error text. Shipped with core `0.1.0a27`. The published `1.0.6` wheel is on the [a23 release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a23) (ends the agy process at the first agy 1.4 subagent step), the published `1.0.5` wheel is on the [a22 release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a22) (no longer disables agy auto-update), and `1.0.4` is on the [a21 release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a21), and `1.0.3` is on the [a19 release page](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a19). The `1.0.0` wheel on the a13 release page still carries the old profile and rejects a Linux HOME. `1.0.2` pins every `_vendor` file, and profile behavior matches `1.0.1`. `1.0.3` lets the vendored `build_command` accept an optional timeout, and the worker applies its own `timeout_seconds` to both its deadline and `--print-timeout`. That field still defaults to 20 seconds and stays at most 20 seconds. It does not read the built-in backend's same-named field of 30 to 3600 seconds. `vendor-source.json` is part of the installed tree, so replacing the package changes the runtime digest and registered connections must be registered again. You can also build it from a fixed source commit as shown below.

Verify the source from a complete Git checkout, then build with a prepared build environment:

```sh
python packages/asterun-plugin-antigravity/scripts/verify-source.py
python -m pip wheel --no-build-isolation --no-deps packages/asterun-plugin-antigravity
```

`vendor-source.json` pins source digests and mechanical transformations, checked against the current source. Set the runner to the absolute path of Python in the plugin environment, followed by `-m asterun_plugin_antigravity.worker`.

Configure the connection with `auth_mode=native_account`. `upstream_version` may be omitted, or set to 1.2.0 or any later version; the worker runs `--version` and requires at least 1.2.0. The worker also runs `--help` and fails with `upstream_feature_missing` if a required flag is missing. Supply the native HOME through a provider_account `native-home:` reference. The options are `bin`, `binary_sha256`, `home`, `model`, and `execution_enabled=true`. Optional `pins` is an absolute path to the same JSON as `ASTERUN_ANTIGRAVITY_PINS` (the host does not pass its environment to the worker). `home` must match the HOME injected by the core. `timeout_seconds` defaults to 20 seconds and must be greater than 0 and at most 20. The host's overall deadline also applies.

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

The standalone wheel and protocol test doubles have been validated. The profile now checks `webm_encoder` and the Playwright cache by structure, with optional pins (see the docs), and that check has been validated live against CLI 1.3.2 on Linux x86_64. Real accounts, subscription charges, official CLI execution, client visibility, and live Linux tasks still await validation.

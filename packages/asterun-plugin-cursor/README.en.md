[中文](README.md) | [English](README.en.md)

# Cursor local Agent plugin

This optional Apache-2.0 plugin runs Cursor local Agent from a separate virtual environment. It pins `cursor-sdk==1.0.31` and `httpx==0.28.1`. See `upstream-lock.json` for API sources and wheel digests, and the [Cursor Python SDK documentation](https://cursor.com/docs/sdk/python) for the upstream interface.

## Configure the connection

Register `cursor.agent` in core configuration v2. Set the runner to the absolute path of Python in the plugin environment, followed by `-m asterun_plugin_cursor.worker`. Pin the manifest, wheel, installed tree, and runner digests. Use `auth_mode=user_api_key` and `upstream_version=1.0.31`; inject `CURSOR_API_KEY` through a provider_account `file:` or `env:` reference. The user must configure the billing pool explicitly.

Required options are `execution_enabled=true`, `runtime=local`, an account-accessible `model`, `tools`, `disallowed_tools`, `mcp_servers={}`, `setting_sources=[]`, `state_root`, `bridge_bin`, and `bridge_sha256`. `state_root` must be a mode-0700 directory owned by the current user and located outside the workspace. Use the verified bridge file from the installed package and its digest. The path must identify a regular file that other users cannot write. The timeout defaults to 15 seconds, with a maximum of 20; the host RPC deadline also applies.

For a minimal text call, set `tools=[]` and `disallowed_tools=["Shell"]`. The plugin reapplies the tool policy whenever it creates or resumes a session. A configuration change prevents resuming the old session. The MCP configuration must currently be empty. The SDK bridge uses local execution, and the plugin runs with the current OS user's permissions.

## Resume, observe, and recover

The plugin provides `task.submit`, `task.get`, `session.resume`, `task.reconcile`, and `task.cancel`. `session.resume(native=true)` verifies the same Agent, latest Run, workspace, and model, then returns `resume_ready`. The next submission with that conversation performs the resume.

Native IDs and connection locks are persisted. Uncertain results remain `unknown`; cancellation is confirmed only after reading back `cancelled`. Cumulative token counts from SDK Runs are deduplicated with stable cursors while preserving their original accounting scope. Back up both core state and the SDK `state_root`.

## Validation status

Default tests use an installed wheel, SDK test doubles, and a local HTTP/Connect simulator. Set `ASTERUN_TEST_CURSOR_WHEELHOUSE` to install the pinned real SDK in an isolated environment and connect it to the simulator.

Real accounts, the official bridge, tool permissions, billing, Desktop display, and Linux await validation. Cloud execution, artifact downloads, interactive approvals, nonempty MCP configuration, and account quota reads are not yet implemented.

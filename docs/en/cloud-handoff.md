# Cloud development

[中文](../cloud-handoff.md) | [English](cloud-handoff.md)

After checking out the [Asterun repository](https://github.com/zonzideka/asterun), inspect the current commit and worktree changes. Then read [AGENTS.md (Chinese reference)](../../AGENTS.md), [STATUS](../../STATUS.en.md), and the [design baseline](design.md). Current interfaces are defined by `src/asterun/` and the [protocol implementation map](protocol/IMPLEMENTATION.md).

```sh
git status --short --branch
git log -3 --oneline
python3 packages/asterun-plugin-antigravity/scripts/verify-source.py
scripts/verify-offline.sh
```

Offline checks use a temporary HOME, state directory, and fake backend. Python 3.11 is the minimum version; install dependencies according to `pyproject.toml` and `requirements-lock.txt`. See [installation](install.md) to reproduce individual tests.

The first-release feature scope is frozen. Work on the assigned fixes to existing behavior and update the relevant documentation. The core and optional plugins live in this repository; callers integrate through CLI/MCP. When changing an interface, document its parameters, results, and compatibility handling in the handoff.

See [source provenance](source-provenance.md) for the current source and public snapshot scope. The Antigravity plugin checks pinned file digests and mechanical transformations within this checkout. Default tests use the current implementation.

Each commit should describe the change, tests actually run, and their results. Include recovery steps for configuration or state migrations. Record live backend-account, native-client, and message-delivery results separately. Deployment follows the instance-switching procedure and preserves existing tasks and native sessions.

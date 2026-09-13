[中文](README.md) | [English](README.en.md)

# Asterun

Asterun runs Agent tasks on your computer or server. Submit work through the CLI, MCP, or another application; the core handles background execution, session links, approvals, results, and recovery. Backends use accounts already authorized on the execution host.

The current version is `0.1.0a13`, licensed under [Apache-2.0](LICENSE). The first release's scope is frozen. See the [release notes](docs/en/release-v0.1.md) for supported features and validation status.

## Get started

Use Python 3.11 or later. Install the downloaded wheel in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install ./asterun-*.whl
.venv/bin/asterun version
source .venv/bin/activate
```

Run the [non-code example](examples/non-code/README.en.md) to try offline submission and acceptance checks. Then follow the [installation guide](docs/en/install.md) to configure your workspaces, backends, and accounts. Start the persistent core for long-running tasks:

```sh
asterun --config /path/to/config.json --state-dir /path/to/state serve
```

Connect to the same instance from another terminal. Replace the workspace and backend names with values from your configuration, then submit a task:

```sh
asterun --state-dir /path/to/state --connect task-submit \
  --workspace demo --backend BACKEND --input - \
  --idempotency-key first-task < /path/to/prompt.txt
asterun --state-dir /path/to/state --connect task-watch TASK_ID --timeout 60
```

For an MCP client, use `asterun --state-dir /path/to/state --connect mcp`. The core keeps running when a watch expires or a client closes. Save the returned task ID to check progress and retrieve the result later.

## Agent capabilities

| Agent | Current capabilities |
|---|---|
| Codex | File reads and edits, commands and tests, native session continuation, command and file-change approvals, turn interruption, and result reconciliation; optional [fixed-scope reads](docs/en/fixed-read-scope.md) and [desktop project association](docs/en/codex-projects.md) |
| Grok | Text tasks, file reads and edits, commands and tests, and iterative execution; the [execution configuration](docs/en/install.md#grok-autonomous-coding) enables the relevant tools |
| Claude | One-shot task invocation, text processing, and workspace file reads; accepts fixed file snapshots as input |
| Antigravity | Text tasks and workspace file reads through a [built-in CLI adapter](docs/en/antigravity.md) or [separate account plugin](docs/en/plugins.md) |
| Cursor plugin | Local Agent task execution, session recovery, and cancellation |
| Jules plugin | Hosted coding tasks, plan retrieval and approval, task feedback, and status queries |

Choose the agents, tool permissions, and collaboration workflow that fit your task. The [release notes](docs/en/release-v0.1.md) list configuration requirements and validation status. Install optional backends through the [plugin guide](docs/en/plugins.md).

## Usage and integration

The [user manual](docs/en/user-manual.md) covers submission, observation, approvals, and recovery. The [PR review guide](docs/en/pr-review.md) provides an optional workflow example with fixed revisions, structured findings, follow-up reviews, and result delivery.

Callers such as GrokBot can deploy Asterun on a cloud computer or invoke a local process over an existing authorized channel. Workspaces, accounts, and native sessions stay on the execution host. Callers track ordinary tasks through task and event records; the [review result consumer](docs/en/review-consumer.md) connects the review inbox to the host's messaging and scheduling facilities.

The optional [Asterun Bot template](https://github.com/zonzideka/asterun/blob/grokbot-template-v0.1.0-rc1/integrations/grokbot/README.en.md) provides GrokBot setup, task tracking, and chat delivery, with its own release package.

For integrations, see [runner-control/v1](docs/en/protocol/IMPLEMENTATION.md) and the [capability profile](docs/en/capability-profile.md).

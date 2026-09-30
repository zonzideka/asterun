# Standard skill entry point

The general Asterun skill lives in `integrations/skills/asterun/`. Copy or package it for a client that supports Agent Skills. Its entry point contains `SKILL.md`, a reference and a Python standard-library helper. The existing core retains execution state, permissions, native sessions, idempotency and recovery. GrokBot chat delivery remains a separate integration.

Install the core and configure a resident instance as described in the [installation guide](install.md). Place the skill in the client's discovery directory, such as a Codex project's `.agents/skills/asterun` or the user's skills directory. Preserve an existing skill with the same name. Copying the skill does not install the core, log into accounts or disable MCP. Explicit `$asterun` invocation is available; implicit selection and other clients require separate verification.

Provide the installed CLI, an existing instance state directory and a private evidence directory outside the core state directory. The helper exposes `discover`, `submit`, `status`, `wait`, `inspect`, `snapshot`, `evaluate`, `repair`, `cancel` and `usage`. Detailed arguments are in the [skill reference](../../integrations/skills/asterun/references/workflow.md).

The helper makes one CLI invocation with `--connect`. It does not open SQLite. Waiting uses the existing compact observer with no events; stdout contains a bounded result and evidence references. Full responses, stderr and requests stay in private files. Evaluation and repair requests reuse the snapshot's complete binding, which the core checks again. The helper does not automatically approve, retry an uncertain mutation, raise budgets or compact a native thread.

Execution, acceptance, independent review, termination and delivery remain distinct. Observation deadlines exit with code 124 while the resident task continues. Client timeouts and invalid responses retain the original intent for reconciliation. External reports remain `external_reported` and cannot satisfy independent review.

After committing the skill source, maintainers run `python3 scripts/package-standard-skill.py --output /path/to/asterun-skill.zip`. The fixed member list excludes runtime state; `asterun/manifest.json` records the source commit and each member's SHA-256. The core source distribution includes the skill and packaging helper. Installing the core wheel does not install user skills or change client settings.

Fake tests can measure visible bytes and workflow boundaries. Controller tokens, real model quality, cache behavior and subscription savings require a matched real comparison. A skill does not automatically remove existing MCP tool descriptions. Repository status and the implementation plan record validation and limitations.

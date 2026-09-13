[中文](README.md) | [English](README.en.md)

# Jules REST plugin

This optional Apache-2.0 plugin calls Jules REST v1alpha. Its standalone wheel requires only the Python standard library. API sources are recorded in `upstream-lock.json`. Authorize the target repository in Jules before use; tasks execute against the remote source and branch.

## Configure and submit a task

Register `google.jules` in core configuration v2. Set the runner to the absolute path of Python in the plugin environment, followed by `-m asterun_plugin_jules.worker`. Pin the manifest, wheel, runtime, and runner digests. Use `auth_mode=user_api_key` and `upstream_version=v1alpha`, and inject `JULES_API_KEY` through a provider_account reference.

The options are `execution_enabled=true`, an authorized `source` such as `sources/github-org-repo`, `starting_branch`, and `state_root`. `state_root` must be a mode-0700 directory owned by the current user and located outside the workspace. The target branch is recorded as `mutable_branch`, so it can change as the remote branch is updated.

`task.submit` creates a task that requires plan approval and returns a native session and URL. Use `task.reconcile` to read the session and its activities. Tasks awaiting plan approval or feedback report that input is required; queued or running tasks remain under observation.

## Review plans and send feedback

Use `capability.invoke` with `remote.plan.inspect` to read a plan, passing the session. To approve it, call `remote.plan.approve` with the same session and `plan_sha256`. Send feedback through `remote.message.send`, passing the session and text. Every operation checks the bound account, workspace, and repository.

Before approval, the plugin rereads the latest plan and source branch. If the digest has changed, review the plan again. The upstream approval API has no conditional digest parameter, so a race remains possible between this check and submission.

## Observe and recover

Each observation reads at most two pages and persists its cursor. Approval reads at most eight pages and pauses if pagination is incomplete. Session polling is spaced at least five seconds apart. Retry-After values from 429/503 responses are stored per account, with a maximum wait of one hour.

The plugin saves an intent before each POST. If the receipt for creation, approval, or feedback is lost, the result remains `unknown` until an operator checks native state. `state_root` stores native receipts and bindings; retain it with core state during recovery.

Cancellation, account quota reads, and actual cost reads are not yet implemented. Budgets use the core's configured local limits and unknown-quota policy.

## Validation status

Default tests use offline REST fixtures. The production endpoint is fixed at `https://jules.googleapis.com/v1alpha/`. Local simulation requires `offline_fixture=true` and the fixed invalid test key.

Real accounts and repository authorization, Jules charges, hosted execution, code results, quality review, and live recovery await validation.

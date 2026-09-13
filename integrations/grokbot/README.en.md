[中文](README.md) | [English](README.en.md)

# Asterun Bot template

This template lets GrokBot invoke the user's chosen Agent through Asterun, follow the task, and return results to the chat that started it. A user can use one Agent for text, documents, or development, or arrange their own implementation, checking, review, and other steps. Agent selection and tool permissions follow the task.

The template includes a [Bot profile](bot-profile.en.md) and three operating skills: [setup](skills/setup/SKILL.md), [task tracking](skills/tasks/SKILL.md), and [recovery and handoff](skills/recovery/SKILL.md). The template version is `0.1.0-rc1`, with core `0.1.0a13` pinned in [release-lock.json](release-lock.json). This directory contains template text and the supporting CLI. All 56 template offline tests pass. Host behavior and task delivery are recorded by scenario below.

## Get the template

The fixed template release entry is the [release page](https://github.com/zonzideka/asterun/releases/tag/grokbot-template-v0.1.0-rc1). Obtain `asterun-grokbot-0.1.0-rc1.zip` and `SHA256SUMS` there, check the complete ZIP against `SHA256SUMS`, then extract it. The included `manifest.json` records the source commit, SHA-256, and byte count for each release file. Record the bundle version and checksum result after downloading.

Save the absolute extraction-root path. Installation, task commands, and routines invoke `scripts/` from that directory. When importing skills into the host's global skill library, retain their fixed-tag public documentation links and use the extraction root saved during setup. The README and Bot profile keep relative links for reading within the bundle.

## Choose where tasks run

| Path | Installation and invocation | Workspaces and accounts |
|---|---|---|
| GrokBot cloud computer | Use `Shell` without `machineId` to install and invoke Asterun, the adapter, and the chosen backends on the selected cloud computer. | Use that host's workspaces, native logins, and private state. |
| User's local computer | Select the user's machine with `ListMachines`, then use `Shell` with that `machineId` to install or invoke the local instance. | Use the local machine's real directories, native logins, and private state. |

Confirm the execution location first. Use `CopyToBox` or `CopyFromBox` when installation material needs to be transferred, following the user's choice of location. Reuse a working Asterun instance when available. For a new environment, follow the [setup skill](skills/setup/SKILL.md): run `scripts/fetch_runtime.py --output-dir ABSOLUTE`, check its result, install the returned wheel in a separate venv, and verify the CLI, core version, and backend configuration. The download script checks the SHA-256 in the release lock; the subsequent explicit pip command performs installation. Sharing a Bot transfers its template text. Recipients still obtain the public template bundle, install code, sign in, and configure their own execution environment.

For coding tasks, bind the workspace to the real project directory on the execution host. For isolated file changes, create a Git worktree and bind its actual directory. Documents and other non-code tasks use a directory chosen by the user; set `allow_non_git: true` for a non-Git workspace. Prepare remote repositories according to the selected backend's workflow, keeping the user's project as the task target.

## Adapter CLI

These interfaces are provided by `scripts/bridge.py`. First enter the template root saved in the setup record: `integrations/grokbot` in a full Git checkout, or the extracted directory in a shared bundle. Replace paths, backend and workspace aliases, and destination with the recipient's verified values. Resolve symbolic links in the selected Asterun entry, verify the real file's source and version, and pass its absolute path as `--asterun`. The adapter uses the standard library. `--ledger` selects this instance's real private directory. Commands return one JSON Envelope on stdout.

```sh
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  init --binding primary --asterun /absolute/venv/bin/asterun \
  --config /absolute/instance/config.json --core-state /absolute/instance/state \
  --location local --workspace PROJECT --backend AGENT \
  --destination PRIVATE_CHAT_ALIAS
```

Read the `init` Envelope and submit only after `ok=true`. On failure, address its specific `error` before continuing with the binding:

```sh
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  submit --binding primary --request-id REQUEST_ID \
  --workspace PROJECT --backend AGENT --input /path/to/prompt.txt
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  poll --binding primary --limit 3
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  inbox --binding primary --limit 3
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  status --binding primary
```

For cloud execution, use `--location host-cloud`. Repeat `--workspace` and `--backend` on `init` as needed. `submit --input -` reads stdin. Add `--conversation-id` for continuation, subject to Asterun's binding checks. Retain `REQUEST_ID` for retries. Replace `PRIVATE_CHAT_ALIAS` with a unique private logical alias for each receiving chat, save it, and reuse it for that chat. The alias associates ledger records. The host's current conversation and routine ownership identify the actual destination, where `SendToUser` sends the message. The available tools do not expose a stable native chat ID or take this alias as a routing argument.

Use `claim` to acquire a specific message. Send only when the Envelope has `ok=true`, `data.already_claimed=false`, and `data.delivery_state=sending`. Call `SendToUser` with `type=text`, `end_turn=false`, and `content` taken unchanged from `claim`'s `data.content`. Keep the turn open so that the host's returned message ID can be recorded with `ack`:

```sh
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  claim --binding primary --delivery-key DELIVERY_KEY
python3 scripts/bridge.py --ledger /path/to/private/ledger \
  ack --binding primary --delivery-key DELIVERY_KEY \
  --message-id HOST_MESSAGE_ID --content-sha256 CONTENT_SHA256
```

For `ack`, set `--message-id` to the opaque ID returned by `SendToUser` and `--content-sha256` to `claim`'s `data.content_sha256`. This records `host_reported_delivered`; confirm chat visibility through actual observation. Skip sending when `already_claimed=true`. If a claimed send has an uncertain outcome, retain `sending`. The host currently has no available history-query interface, so inspect the actual outcome or obtain the user's explicit confirmation that nothing was sent. Then use the same ledger with `resolve --binding primary --delivery-key DELIVERY_KEY --not-sent` and claim again.

## Tracking and delivery

After submission, save the returned task/run/conversation references and current chat association. The persistent Asterun core executes long-running work. Create a native routine with `update_state`, `target=routine`, and `action=create`, using the standard five-field cron schedule `*/5 * * * *`. Use `update` or `resume` for an existing routine. A minimal probe with this schedule fired automatically and displayed a marker in the originating chat.

Keep the routine in the original chat with the same `binding`. Each wake runs one `poll --limit 3` on the bound execution host, then reads `inbox --limit 3`. Process that batch using `claim`, `SendToUser`, and `ack` as described above. Stay quiet while ordinary state is unchanged. Complete the final poll, status check, and any required routine pause before ending the turn.

After delivery, run `poll` again to check the current run, then read `status`. Retain the routine while `needs_followup=true`, including running tasks, pending delivery, and unresolved `sending` records. When it is `false`, use `update_state` with `target=routine` and `action=pause` for that binding's routine. Resume after submitting new work. If the user ends tracking, preserve task and delivery records.

Recipients create or record their own routine, chat association, machine selection, template extraction root, and private ledger. Keep the `Shell` execution location, the current `SendToUser` chat, and the binding's destination aligned.

## Workflows and acceptance

Organize work around the user's objective. Use the [PR review recipe](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/en/pr-review.md) when a task calls for PR review, and configured checks or independent quality workflows when required. Ordinary tasks use the task interface directly.

Share generic template text, public installation references, and examples. Keep account credentials, native sessions, real project paths, runtime records, routine identifiers, and chat bindings in each user's private environment.

On 2026-09-13, verification on the current account's cloud computer covered copying installation material, checking hashes, downloading the pinned a13 wheel, installing it in a separate Python 3.13.5 venv, and reading its version. Repeating a fake task with the same request key returned the same task and run references. After navigating away from the chat page, a cron routine woke the Bot and delivered the result to the original chat. The acknowledgement recorded `host_reported_delivered`; a subsequent poll returned `needs_followup=false`, and the routine was paused. This verifies the cloud core, adapter, and frontend delivery with a fake backend.

Local Grok Build has completed a text task in the real project directory, and its summary has been checked against the input. The host receipt records `host_reported_delivered`, and the final poll requires no follow-up. Subsequent native UI checks confirmed the result, receipt, and summary in the original chat, along with all three temporary routines being paused. Cloud model invocation, application-restart recovery, native sharing now being prepared, and independent recipient acceptance remain in the [acceptance record](acceptance.md). Bot copies and cloud computers under the same account retain that account's environment; independent new-user verification is still pending. See the [release notes](https://github.com/zonzideka/asterun/blob/v0.1.0a13/docs/en/release-v0.1.md) for backend support.

The fixed release ZIP is public and has passed anonymous-download, checksum, and complete manifest checks. Its documentation retains the record at release; the current acceptance documents separately record the later local UI and routine-pause confirmations.

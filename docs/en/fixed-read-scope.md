[中文](../fixed-read-scope.md) | [English](fixed-read-scope.md)

# Read a fixed set of files

New Codex PR reviews use `native_fixed_scope` by default. Once a fixed snapshot and diff have been submitted, the model uses `asterun_read` to list files, read lines, and search for literal text. The read scope is bound to the task, so reads within it do not require individual shell approvals.

## Choose a read mode

`review-prepare --read-mode auto` selects fixed-scope reads for a new Codex review. Use `native` to require this entry point explicitly, or `manual` for individual native approvals. For an existing attempt, `auto` retains its original mode. Create a new attempt to switch modes.

Generic CLI JSON requests and MCP `task_submit` also accept `read_scope`, containing a workspace-relative `manifest_path` and `manifest_sha256`. The manifest uses `asterun-read-scope/v1`; each file has a virtual `name`, relative `path`, byte size, and SHA-256 digest. The scope is included in the task input hash and stays the same throughout a native session. Resuming a session reinstalls the local read handler.

## File validation and pagination

Asterun opens directories and regular files one path component at a time. It validates the manifest on every call and checks each file's size, digest, and changes during the read. The PR recipe also verifies that the manifest covers exactly the review's source snapshot and diff. Symlinks, out-of-scope paths, and special files return errors.

A scope may contain up to 20,001 files totaling 96 MiB. The manifest is limited to 8 MiB and each file to 64 MiB. Text must be valid UTF-8. Each tool response is limited to 64 KiB; use the returned continuation fields to read further:

| Operation | Continuation fields |
|---|---|
| `list` | `next_offset` |
| `read` | `next_start_line`, `next_start_column` |
| `search` | `next_cursor` |

Line numbers start at 1 and character columns at 0. Search uses literal matching and returns one result per matching line. It validates every selected file and reports corrupt or unreadable files explicitly.

## Native permissions and validation status

Before launch, Asterun exports the required schemas in an isolated temporary HOME, then checks the actual thread configuration. The process and thread disable shell access, writes, external tools, MCP, apps, plugins, web access, and automatic repository instruction sources. The prompt is sent after these settings have been verified. Global AGENTS instructions in the native HOME are still inherited; tool access remains restricted to the fixed scope. Incompatible APIs or configuration fail the task. Uncertain remote results retain their reconciliation state.

Ordinary tasks, older reviews, and other backends keep their existing approval paths. The independent quality reviewer uses its own `snapshot_only_no_tools` configuration. Use of the new read entry point by a real model, and its display in the native client, await live validation. See the [release notes](release-v0.1.md).

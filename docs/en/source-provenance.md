# Source provenance

[中文](../source-provenance.md) | [English](source-provenance.md)

The initial source snapshot contains the current core, optional plugins, tests, configuration examples, and Chinese and English documentation. Earlier development branches, runtime records, and migration material are archived separately by the maintainer. The public snapshot starts from the current implementation.

The 102 runtime files in the core and plugins retain their source-version bytes. Paths and SHA-256 values are listed in [SOURCE-PROVENANCE.json](../../SOURCE-PROVENANCE.json). The manifest records content provenance; update it with the relevant test results when runtime code changes.

Three vendored Antigravity files can be reproduced from the core source in this checkout. `vendor-source.json` records the original source identifier, source digests, mechanical transformations, and target digests. The verifier also pins the manifest's own digest, checks the current files, and reapplies each transformation:

```sh
python3 packages/asterun-plugin-antigravity/scripts/verify-source.py
```

Verification uses the current source and fixed digests, without requiring old Git objects. In its output, `source_commit` retains the historical source identifier and `source_git_object_checked=false` identifies content-based verification. `original_sources_unchanged` means the current source files match their pinned digests.

The default offline entry point is `scripts/verify-offline.sh`. It retains all runtime tests and verifies plugin provenance. Protocol specifications and fixed schemas retain their original bytes; see the [implementation map](protocol/IMPLEMENTATION.md) for current coverage.

The public snapshot is based on `c19535e`, with runtime code from `37bc705`. Their full identifiers appear in the manifest as `source_snapshot_revision` and `runtime_source_revision`. All 102 test files match the source snapshot. The full offline suite on Python 3.11.16 completed with 2002 passed and 4 skipped, using temporary state and offline backend fixtures. The core and three plugins produced 4 packages and 8 wheel/sdist archives, with 34 packaging checks passed. All 4 sdists were rebuilt and all 4 wheels installed offline. See the [release notes](release-v0.1.md) for current support and live validation coverage.

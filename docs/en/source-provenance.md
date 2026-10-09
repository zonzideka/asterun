# Source provenance

[中文](../source-provenance.md) | [English](source-provenance.md)

The initial source snapshot contains the current core, optional plugins, tests, configuration examples, and Chinese and English documentation. Earlier development branches, runtime records, and migration material are archived separately by the maintainer. The public snapshot starts from the current implementation.

The 102 runtime files in the core and plugins are listed in [SOURCE-PROVENANCE.json](../../SOURCE-PROVENANCE.json), each with its path and SHA-256. The manifest records content provenance; when a runtime file changes, update its digest together with the relevant test results. `runtime_source_revision` remains the public snapshot `37bc705`, and the list still has the 102 entries from that snapshot. It does not enumerate modules added later. Digests for the listed runtime files touched by this change, and for the Antigravity `manifest.json` and `vendor-source.json`, now match the current source and no longer match that revision. The new `src/asterun/child_env.py` is not one of those 102 entries.

Every file under the Antigravity plugin's `_vendor` directory must appear in `vendor-source.json`. `profile.py`, `runtime.py`, and `command.py` are mechanical transforms of the core source at commit `9610e99f95a5614f6f8e1b0f10bf87d9d4f67f71`. `compat.py` and `__init__.py` are local plugin copies pinned by their target digests. The verifier pins the manifest's own digest, covers every file in the directory, and, when the checkout contains `.git`, confirms that commit and its source blobs. A failed check exits nonzero and does not rely on `assert`. Without Git metadata it still checks the working tree against the pinned digests, and `source_git_object_checked` is then false.

```sh
python3 packages/asterun-plugin-antigravity/scripts/verify-source.py
```

`original_sources_unchanged` means the current source files match their pinned digests. If the copy changes relative to main, the plugin pyproject version and the manifest `plugin_version` must be identical and each must be strictly greater than its baseline under PEP 440. A prerelease does not pass even when it sorts above the baseline, such as `1.0.2a1` after `1.0.1`. A downgrade does not pass either. CI also builds and installs the three plugin wheels, then imports them, registers a pin, and calls `plugin.describe` through the isolated launch.

The default offline entry point is `scripts/verify-offline.sh`. It retains all runtime tests and verifies plugin provenance. Protocol specifications and fixed schemas retain their original bytes; see the [implementation map](protocol/IMPLEMENTATION.md) for current coverage.

The public snapshot is based on `c19535e`, with runtime code from `37bc705`. Their full identifiers appear in the manifest as `source_snapshot_revision` and `runtime_source_revision`. All 102 test files match the source snapshot. The full offline suite on Python 3.11.16 completed with 2002 passed and 4 skipped, using temporary state and offline backend fixtures. The core and three plugins produced 4 packages and 8 wheel/sdist archives, with 34 packaging checks passed. All 4 sdists were rebuilt and all 4 wheels installed offline. See the [release notes](release-v0.1.md) for current support and live validation coverage.

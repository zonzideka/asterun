# Initial source scope

[中文](../repository-audit.md) | [English](repository-audit.md)

The initial source snapshot contains the Asterun core, separate plugins, tests, configuration examples, protocol material, and Chinese and English documentation under Apache-2.0. Third-party dependencies retain their own licenses.

The snapshot preserves the original bytes of 102 runtime files. See [source provenance](source-provenance.md) for the content manifest and reproducible plugin verification. Tests use temporary environments, fake backends, or protocol fixtures.

Historical deployment and migration records, native sessions, and the legacy reference tree are retained in the maintainer's separate archive. This snapshot uses generic path examples; deployments manage accounts and runtime state. `.gitignore` excludes credentials, environment files, and SQLite state while retaining public examples.

Release checks validate wheel/sdist members, documentation links, personal paths, and license text. Final content scans, offline tests, and package rebuilds for this snapshot should be completed and recorded before publication. Existing validation status is described in the [release notes](release-v0.1.md).

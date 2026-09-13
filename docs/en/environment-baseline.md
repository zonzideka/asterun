# Development and test environments

[中文](../environment-baseline.md) | [English](environment-baseline.md)

Asterun requires Python 3.11 or later. Runtime dependencies and build configuration are in [pyproject.toml](../../pyproject.toml); [requirements-lock.txt](../../requirements-lock.txt) pins offline test dependencies. GitHub Actions runs the default offline checks on Python 3.11 and 3.12.

```sh
scripts/verify-offline.sh
```

The script creates a temporary virtual environment, HOME, state directory, and workspace, then verifies behavior using fake backends or protocol fixtures. Live backend calls are controlled by the corresponding configuration and environment switches. Test reports must identify the environment and scope checked.

The initial cloud baseline on 2026-09-07 was Linux x86_64 with Python 3.12.3. Later source snapshots were also verified on macOS with Python 3.11.16 and 3.14.0. See [STATUS](../../STATUS.en.md) for the latest results.

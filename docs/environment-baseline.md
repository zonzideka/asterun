# 开发与测试环境

[中文](environment-baseline.md) | [English](en/environment-baseline.md)

Asterun 使用 Python 3.11 及以上版本。运行依赖和构建配置见 [pyproject.toml](../pyproject.toml)，离线测试依赖由 [requirements-lock.txt](../requirements-lock.txt) 固定。GitHub Actions 在 Python 3.11 和 3.12 上执行默认离线检查。

```sh
scripts/verify-offline.sh
```

脚本创建临时虚拟环境、HOME、状态目录和工作区，使用 fake 或协议替身验证。真实后端调用由对应配置和环境开关控制。测试记录须写明运行环境和检查范围。

2026-09-07 的初始云端基线为 Linux x86_64、Python 3.12.3。后续固定源码也在 macOS Python 3.11.16 和 3.14.0 上验证，最新结果见 [STATUS.md](../STATUS.md)。

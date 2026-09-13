# ADR 0001：语言、打包与依赖锁定

日期：2026-09-07。状态：已采纳（A0-01/A1-01）。

## 决策

延续 Python，最低版本 3.11，本环境验证 3.12.3。包布局为 `src/asterun/`，用 `pyproject.toml` + setuptools 构建。首轮运行时依赖为空，只使用标准库。测试额外依赖仅钉死 `pytest==8.3.5`。锁定文件是 [requirements-lock.txt](../../requirements-lock.txt)，记录测试/构建销钉，不引入 uv 或 poetry。

配置使用 JSON 而不是 YAML，避免运行时依赖 PyYAML。状态在 A1-02 使用显式目录下的 JSON 文件，仅为 CLI 连续调用服务；不把它写成 A1-03 的 SQLite 事务库。

## 替代方案

- 使用 uv 或 poetry：本云端环境未预装，增加安装面，收益仅限锁文件格式。
- 运行时引入 pydantic：schema 更严，但首轮契约可用 dataclass 表达，减少依赖评估。
- 配置用 TOML：与 `pyproject.toml` 一致，但用户配置更常见 JSON，标准库 `tomllib` 只读也够用；JSON 对 CLI `--request` 更统一。

## 兼容与回退

最低 3.11 覆盖旧项目起点和当前云端 3.12。若后续必须加入运行时依赖，先改本 ADR、更新锁文件，再改代码。不能把作者机器上的 site-packages 当作可复现环境。回退方式：删除 `src/asterun/` 与包装文件，仓库回到纯文档状态。

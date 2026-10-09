# 源码来源

[中文](source-provenance.md) | [English](en/source-provenance.md)

首版源码快照保留当前核心、可选插件、测试、配置样例和中英文文档。早期开发分支、运行记录与迁移材料由维护者单独归档，公开快照从当前实现开始。

核心和插件的 102 个运行文件列在 [SOURCE-PROVENANCE.json](../SOURCE-PROVENANCE.json)，每项带有路径和 SHA-256。该清单记录内容来源；运行文件变更时连同测试结果更新对应摘要。清单里的 `runtime_source_revision` 仍是公开快照 `37bc705`，条目数仍是当时的 102 个，不逐一收录其后新增的模块。本次改动过的清单内运行文件，以及 Antigravity `manifest.json`、`vendor-source.json`，摘要已按当前源码更新，不再等于该修订。新增的 `src/asterun/child_env.py` 不在这 102 项里。

Antigravity 插件的 `_vendor` 必须与 `vendor-source.json` 一一对应。`profile.py`、`runtime.py` 和 `command.py` 由提交 `9610e99f95a5614f6f8e1b0f10bf87d9d4f67f71` 的核心源码经固定机械变换得到；`compat.py` 与 `__init__.py` 是插件本地副本，只钉定目标摘要。核验脚本固定清单自身摘要，覆盖目录中的全部文件，并在检出包含 `.git` 时确认该提交存在、核对来源 blob。检查失败以非零状态退出，不依赖 `assert`。没有 Git 元数据时仍核对工作区文件与固定摘要，此时 `source_git_object_checked` 为 false。

```sh
python3 packages/asterun-plugin-antigravity/scripts/verify-source.py
```

`original_sources_unchanged` 表示当前源文件与钉定摘要一致。副本内容相对 main 发生变化时，插件版本与 manifest 的 `plugin_version` 必须一起升高。CI 还会构建并安装三个插件 wheel，做导入和注册冒烟。

默认离线入口为 `scripts/verify-offline.sh`，保留全部运行测试并执行插件来源核验。协议规范及固定 Schema 保留原始字节，当前实现范围见[协议实现表](protocol/IMPLEMENTATION.md)。

公开快照来源为 `c19535e`，运行代码来源为 `37bc705`，完整标识分别记录为清单中的 `source_snapshot_revision` 和 `runtime_source_revision`。全部 102 个测试文件与快照来源一致。Python 3.11.16 完整离线回归得到 2002 通过、4 跳过，使用临时状态与离线后端夹具。核心与三个插件共 4 个包、8 份 wheel/sdist 通过 34 项包装检查，4 份 sdist 重建与 4 个 wheel 离线安装均已验收。当前支持情况与现场验收范围见[发行说明](release-v0.1.md)。

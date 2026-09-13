# 源码来源

[中文](source-provenance.md) | [English](en/source-provenance.md)

首版源码快照保留当前核心、可选插件、测试、配置样例和中英文文档。早期开发分支、运行记录与迁移材料由维护者单独归档，公开快照从当前实现开始。

核心和插件的 102 个运行文件保持源版本字节，路径与 SHA-256 列在 [SOURCE-PROVENANCE.json](../SOURCE-PROVENANCE.json)。该清单记录内容来源；后续代码变更应连同测试结果更新。

Antigravity 插件的三个移植文件可以从本仓核心源码复现。`vendor-source.json` 保存原始来源标识、源文件摘要、机械变换和目标摘要；核验脚本同时固定该清单自身摘要，逐项检查当前文件并重新执行变换：

```sh
python3 packages/asterun-plugin-antigravity/scripts/verify-source.py
```

核验依赖当前源码与固定摘要，无需读取旧 Git 对象。输出中的 `source_commit` 保留历史来源标识，`source_git_object_checked=false` 说明本次使用内容核验。`original_sources_unchanged` 表示当前源文件与固定摘要一致。

默认离线入口为 `scripts/verify-offline.sh`，保留全部运行测试并执行插件来源核验。协议规范及固定 Schema 保留原始字节，当前实现范围见[协议实现表](protocol/IMPLEMENTATION.md)。

公开快照来源为 `c19535e`，运行代码来源为 `37bc705`，完整标识分别记录为清单中的 `source_snapshot_revision` 和 `runtime_source_revision`。全部 102 个测试文件与快照来源一致。Python 3.11.16 完整离线回归得到 2002 通过、4 跳过，使用临时状态与离线后端夹具。核心与三个插件共 4 个包、8 份 wheel/sdist 通过 34 项包装检查，4 份 sdist 重建与 4 个 wheel 离线安装均已验收。当前支持情况与现场验收范围见[发行说明](release-v0.1.md)。

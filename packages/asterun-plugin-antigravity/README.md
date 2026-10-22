[中文](README.md) | [English](README.en.md)

# Antigravity 账户插件

本插件通过 Antigravity CLI 1.2.0 或更高版本使用原生账户，采用 Apache-2.0。插件安装在独立环境，wheel 仅依赖 Python 标准库。Asterun 核心通过显式 manifest 和安装摘要加载插件，外部配置标识为 `google.antigravity-cli`。

## 构建和配置

当前源码版本为 `1.0.8`：被 profile deny 规则硬拒绝的工具调用不再单独判失败，模型给出最终回答即为成功（记 `policy_denied_count`）；无人值守下的软拒绝改报 `ANTIGRAVITY_PERMISSION_REVIEW_REQUIRED`（记 `soft_denied_tools`）。`1.0.7`：在 agy 结果为 ERROR 时，按严格规则把 429 额度耗尽与单纯限流分开，只把分类（kind、状态码、来源、供应商）放进原生输出，不保存错误原文。随核心 `0.1.0a27` 发行。已发布的 `1.0.6` wheel 在 [a23 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a23)（一出现 agy 1.4 的子代理步骤就结束 agy 进程），`1.0.5` wheel 在 [a22 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a22)（不再关闭 agy 自动更新），`1.0.4` 在 [a21 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a21)，`1.0.3` 在 [a19 发行页](https://github.com/zonzideka/asterun/releases/tag/v0.1.0a19)。a13 的 `1.0.0` wheel 仍带旧 profile，会拒绝 Linux HOME。`1.0.2` 补上全部 `_vendor` 文件的来源钉定，profile 行为与 `1.0.1` 相同。`1.0.3` 让 vendored `build_command` 接受可选超时，并把 worker 自己的 `timeout_seconds` 同时用于运行期限和 `--print-timeout`。该字段仍默认 20 秒、最多 20 秒，不读取内置后端 30 到 3600 秒的同名字段。`vendor-source.json` 属于安装内容，换装后运行树摘要会变，已注册连接需重新注册。也可按下面的步骤从固定源码构建。

从完整 Git 检出核验来源，再使用已准备的构建环境打包：

```sh
python packages/asterun-plugin-antigravity/scripts/verify-source.py
python -m pip wheel --no-build-isolation --no-deps packages/asterun-plugin-antigravity
```

`vendor-source.json` 固定来源和机械转换。runner 使用插件环境中 Python 的绝对路径，并附加 `-m asterun_plugin_antigravity.worker`。

连接配置使用 `auth_mode=native_account`。`upstream_version` 可省略，或写 1.2.0 及以上任一版本；worker 运行 `--version` 并要求不低于 1.2.0，也会运行 `--help`，缺少必需参数时以 `upstream_feature_missing` 失败。通过 provider_account 的 `native-home:` 引用指定原生 HOME。options 包含 `bin`、`binary_sha256`、`home`、`model` 和 `execution_enabled=true`，可选 `pins`（与 `ASTERUN_ANTIGRAVITY_PINS` 相同 JSON 的绝对路径；宿主不会把自身环境传给 worker）。`home` 须与核心注入的 HOME 一致。`timeout_seconds` 默认为 20 秒，可设为大于 0 且不超过 20 的值，同时受宿主总期限约束。

使用独立入口准备 HOME 并绑定读取范围：

```sh
asterun-antigravity-plugin prepare --home /absolute/private/native-home
asterun-antigravity-plugin bind-workspace \
  --home /absolute/private/native-home --workspace /absolute/workspace
```

登录和 profile 配置见 [Antigravity 用法](https://github.com/zonzideka/asterun/blob/main/docs/antigravity.md)。执行时固定二进制、模型和工作区，提示通过 stdin 传递。profile 授权读取，拒绝写入、命令、Web 和 MCP，并关闭 G1 credits。原生 CLI 继续保存自身日志和会话。

## 结果和恢复

成功结果须同时具备有效 init、完整步骤、匹配终态和正常退出回执；工具错误和软拒绝计入失败。结果保留原生 session ID，用量标记为 `session_cumulative`。

当前预览包支持单次同步执行。跨 RPC 的原生续接、观察、取消、审批、对账和额度查询尚未接入。中途失联时结果可能保留 `unknown`，需人工核对原生状态并保留预算预留。余额未知时，核心按有界策略和显式 overage 配置决定是否准入。

独立 wheel 和协议替身验证已完成。profile 现按结构检查 `webm_encoder` 和 Playwright 缓存，并支持可选字节钉定（见文档）；已在 Linux x86_64 上对照 CLI 1.3.2 完成现场核验。真实账户、订阅扣量、官方 CLI 执行、客户端显示以及 Linux 现场任务仍待验收。

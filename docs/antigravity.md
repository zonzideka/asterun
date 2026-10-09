[中文](antigravity.md) | [English](en/antigravity.md)

# 使用 Antigravity CLI

Asterun 通过官方 `agy` CLI 的 `stream-json` 调用用户的原生账户，当前适配基线为 CLI 1.2.0。`workspace-read-v1` profile 支持文本任务和指定工作区读取，每个 Run 创建新的进程和原生会话。macOS 已验证文本执行、文件读取和权限拒绝。Linux x86_64 上，官方 CLI 1.2.0 的 `webm_encoder` 与 `~/.cache/ms-playwright-go` 已纳入按平台固定的 profile 检查；该平台的真实文本执行、文件读取和权限拒绝仍待现场验收。其他系统与其他 CLI 版本需单独核验后才能加入清单。

## 准备原生 HOME

先安装[官方 CLI](https://antigravity.google/docs/cli/install/)，核对程序的绝对路径。Asterun 按配置 `bin`、`ANTIGRAVITY_BIN`、PATH、平台标准安装位置的顺序查找程序。

下面使用临时示例目录。长期运行时，请换成私有持久目录，分别存放原生 HOME 和工作区：

```sh
umask 077
mkdir -p /private/tmp/asterun-agy-demo/workspace
asterun-antigravity prepare --home /private/tmp/asterun-agy-demo/native-home
asterun-antigravity inspect --home /private/tmp/asterun-agy-demo/native-home
```

`prepare` 初始化新建或空 HOME；目录已有内容时，会按 profile 核验。然后在该 HOME 中启动原生 CLI，完成 Google 登录。将下面的程序路径替换为实际安装路径：

```sh
cd /private/tmp/asterun-agy-demo/workspace
/usr/bin/env -i \
  HOME=/private/tmp/asterun-agy-demo/native-home \
  PATH=/usr/bin:/bin:/usr/sbin:/sbin \
  /absolute/bin/agy
```

若 CLI 询问工作区信任，选择这个独立目录。当前 profile 接受一个有效的 `trustedWorkspaces` 绑定，后续执行目录须与其一致。登录完成后退出 CLI，再运行 `inspect` 核对配置。

接着绑定读取范围：

```sh
asterun-antigravity bind-workspace \
  --home /private/tmp/asterun-agy-demo/native-home \
  --workspace /private/tmp/asterun-agy-demo/workspace
```

该命令添加精确的 `read_file` allow，并保留六项 deny。读取绑定、原生信任和执行工作区须指向同一目录。启动前会检查工作区及其祖先；存在 `.agents`、`.agent`、`.gemini`、`AGENTS.md` 或 `GEMINI.md` 时，需先准备独立源码快照，再绑定该快照目录。

## 配置并提交任务

将下例保存为 `/private/tmp/asterun-agy-demo/config.json`，替换 `bin` 和模型。模型 ID 通过同一原生账户的 `agy models` 核对：

```json
{
  "schema_version": 1,
  "revision": 1,
  "workspaces": {
    "agy-demo": {
      "root": "/private/tmp/asterun-agy-demo/workspace",
      "allow_non_git": true
    }
  },
  "backends": {
    "antigravity": {
      "kind": "antigravity",
      "enabled": true,
      "bin": "/absolute/bin/agy",
      "home": "/private/tmp/asterun-agy-demo/native-home",
      "model": "MODEL_ID_FROM_AGY_MODELS"
    }
  },
  "default_backend": "antigravity"
}
```

可选字段 `timeout_seconds` 同时限制原生 `--print-timeout` 和适配器等待时间。省略时为 300 秒，命令行仍是 `5m`。允许 30 到 3600 的整数；能被 60 整除时传给 CLI 的是分钟，例如 600 秒为 `10m`，否则是秒，例如 90 秒为 `90s`。v1 写在 `backends.<name>`，v2 写在内置 `google.antigravity-cli` 连接的 `options`。外部插件不读取这个内置字段。插件自己的 `timeout_seconds` 默认 20 秒，必须大于 0 且不超过 20，同时用于 worker 期限和 `--print-timeout`。省略时插件命令行是 `20s`。小数向上取整后只传给 CLI，worker 仍按原小数截止。

确认安装、profile 和模型后，在核心服务进程中开启执行。以下操作会使用原生账户额度：

```sh
ASTERUN_RUN_ANTIGRAVITY=1 asterun \
  --config /private/tmp/asterun-agy-demo/config.json \
  --state-dir /private/tmp/asterun-agy-demo/state serve
```

在另一终端提交任务，再用返回的任务 ID 观察结果：

```sh
asterun --state-dir /private/tmp/asterun-agy-demo/state --connect \
  task-submit --workspace agy-demo --backend antigravity \
  --text '回复 ASTERUN_AGY_OK。' --idempotency-key agy-demo-text-1
asterun --state-dir /private/tmp/asterun-agy-demo/state --connect \
  task-watch TASK_ID --timeout 60 --request-timeout 5
```

`backend-inspect` 和 `connection-verify` 检查安装与配置，登录状态仍记为 `not_tested`。提交重试使用原幂等键；观察到期后继续查询同一任务。

## 权限和结果的含义

profile 使用 `request-review`，只授予精确工作区的文件读取权限，拒绝写文件、执行命令、网页读取与执行、unsandboxed 和 MCP 工具。原生 CLI 仍会保存自身日志、会话和缓存。上述权限由原生策略执行，OS 强制隔离待验收。

适配器使用专用 HOME，清理继承的认证和配置覆盖，并核对已知状态形状。CLI 1.2.0 的内置技能文件在已核验的 macOS 与 Linux x86_64 构建上使用同一份固定清单。`webm_encoder` 按平台固定大小和 SHA-256，不接受未登记摘要：macOS 沿用已核验的 12781442 字节、`9cf13e875e7ffb9ef3fe1fc1a177c744bd8ee5a6651a2ec47cda307a009e8d11`；Linux x86_64 使用官方 1.2.0 构建的 17056035 字节、`45c1b1edd50159fbd4eac95ffd82df97a79b8c345fc43408ddae09230a304ed6`。Playwright 缓存只接受本平台的空目录，macOS 为 `Library/Caches/ms-playwright-go/1.57.0`，Linux 为 `.cache/ms-playwright-go/1.57.0`。要支持新的 CLI 版本，需要为该版本单独登记内置文件、各平台编码器摘要、缓存形状，以及插件的 `upstream_version` 门禁，不能放宽现有 1.2.0 清单。认证保留在原生账户中，`useG1Credits=false`。

公开结果包含 `agent_response`、工作区、模型、权限和选定统计。成功结果须同时具备有效 init、完整步骤、匹配的原生终态和正常退出回执；工具错误和软拒绝计入失败。原生 conversation ID 保存在 `native.backend_session_id`，`native.usage_scope=session` 表示会话累计用量。

当前每次独立执行创建新 Asterun 会话；原生续接、恢复对账和机器审批桥尚未接入。取消会尝试中断原生进程，收到原生 CANCELED/INTERRUPTED 和退出回执后记录 `cancelled`；结果不明时保留 `pending_reconcile`，由操作者核对原生状态。

## 迁移旧权限配置

早期 strict profile 需显式迁移。先停止使用该 HOME 的 CLI 和任务，再选择 HOME、工作区之外的私有备份目录：

```sh
mkdir -m 700 /private/tmp/asterun-agy-demo/migration
asterun-antigravity upgrade-permissions \
  --home /private/tmp/asterun-agy-demo/native-home \
  --backup /private/tmp/asterun-agy-demo/migration/settings.strict.json
```

迁移会校验旧配置、备份原始字节，再原子更新 `toolPermission` 并返回摘要。已有备份、配置偏离或并发变化时会停止覆盖。需要回退时，先停止 CLI，将备份原子恢复到 `.gemini/antigravity-cli/settings.json`，保持 0600 权限，并使用支持 strict 的旧适配器。

原生 CLI 的细节见 [headless](https://antigravity.google/docs/cli/headless/)、[设置](https://antigravity.google/docs/cli/settings/)、[权限](https://antigravity.google/docs/cli/permissions/)和[额度](https://antigravity.google/docs/cli/credits/)文档。

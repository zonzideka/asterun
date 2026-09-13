# 本包验证记录

日期：2026-09-10。对象：`runner-control/v1 / 2026-09-10.draft1`。

## 已实际执行并通过

命令：

```sh
python -m pytest -q
tsc --noEmit
```

自动测试输出：`85 passed in 1.30s`。TypeScript 编译器退出码为 0、无类型错误。

执行环境：Python 3.13.5；jsonschema 4.26.0；pytest 9.0.2；TypeScript 5.8.3。测试依赖范围写在 requirements-test.txt，以上是本次实际版本，不表示已经穷尽验证该区间内所有版本。

已覆盖：JSON Schema Draft 2020-12 结构；23 个独立合成 JSON 样例；九类核心对象的未知字段拒绝；指定命令/证据/审批结构约束；安全整数、时间、金额形状；状态机枚举一致；一个真实文件内容哈希；部分暂停/成功/失败守卫；持久控制意图的恢复规则；CAS；内存原子幂等受理；内存并发预算预留、未知占用与只结算一次；版本化验证证据；授权摘要/范围/有效期参考判定。

另外，`interfaces/types.ts` 与 `interfaces/backend-adapter.ts` 已在 strict/noEmit 下通过静态类型检查。

## 没有执行或提供的内容

没有实现或启动生产 runner、HTTP/MCP 服务、数据库事务/持久 outbox、身份认证、OS 沙箱、网络调用或后端适配器；没有连接用户的 Codex、Grok Build/Grok Bot、super.engineering 或 Desktop；没有执行真实进程终止、断电、网络分区、费用或远端幂等测试。

`reference/guards.py` 仅为内存参考模型，只覆盖规范的一部分守卫，不提供跨进程/崩溃后保证。Schema 通过只证明形状符合约定，不证明跨对象引用、来源真实性、权限和事实正确性。

RFC 8785 JCS 被选为规范摘要算法，但本包没有提供/验证生产 JCS 实现。样例中的 a/b/c 摘要为结构占位；只有 Artifact.json 对 content.txt 的 SHA-256 是实际内容测试。

静态类型检查不能验证供应商 API 存在、身份已登录、上游版本兼容、Desktop 项目可见性或真正的暂停/续接行为。相关公开资料仅标 documented；完整真实环境验收见 CONFORMANCE.md 的 34 项清单。

## 文件完整性

SHA256SUMS 列出包内文件的内容哈希（不含该清单自身），用于传输后的完整性检查，不是作者签名、可信身份或安全审计证明。ZIP 包是本目录文件的交付副本。

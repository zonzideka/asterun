# ADR 0002：SQLite 意图事务与单实例锁

日期：2026-09-07。状态：已采纳（A1-03）。

## 决策

有副作用的 `task.submit` 先在同一事务写入 operation、task、run，提交成功后再调用后端。同一主体、动作、目标和幂等键：输入哈希相同则复用已有派发；不同则 `IDEMPOTENCY_CONFLICT`。意图写盘失败返回 `INTENT_PERSIST_FAILED`，不调用后端。这与旧 agent-runner「WAL 失败不阻塞派发」相反，是有意收紧。

同一 `--state-dir` 用 `fcntl` 排它锁；第二实例得到 `INSTANCE_LOCKED`。CLI 与 stdio MCP 都通过 `Application.from_paths` 取得这把锁和同一份 SQLite。

## 回退

删除 `sqlite_store.py` / `instance.py` / `mcp_server.py`，`from_paths` 改回 A1-02 的 JSON 文件存储。已有 `asterun.sqlite` 可保留作废数据，不自动删除用户文件。

"""按原生消息阶段选择最终输出，不从任意文本中猜测 JSON。"""
from __future__ import annotations


def final_text(items: list[dict], fallback: str = "") -> str:
    messages = [item for item in items if isinstance(item, dict) and item.get("type") == "agentMessage"]
    if not messages:
        return fallback
    # 明确提供阶段的上游不能把 commentary 或未知阶段当作最终答复。
    if any(item.get("phase") is not None for item in messages):
        messages = [item for item in messages if item.get("phase") == "final_answer"]
    return "\n".join(item["text"] for item in messages if isinstance(item.get("text"), str))


class MessageOutput:
    def __init__(self):
        self.items = {}

    def observe(self, method, params):
        candidates = []
        if method in {"item/started", "item/completed"}:
            candidates = [params.get("item")]
        elif method == "turn/completed":
            candidates = (params.get("turn") or {}).get("items", [])
        for item in candidates:
            if not isinstance(item, dict) or item.get("type") != "agentMessage":
                continue
            key = item.get("id")
            if not isinstance(key, str) or not key:
                # 无 ID 的旧版 turn 快照依然可以作为完整结果来源。
                if method != "turn/completed":
                    continue
                key = "legacy-" + str(len(self.items))
            if method != "item/started":
                self.items[key] = dict(item)

    def text(self, fallback=""):
        return final_text(list(self.items.values()), fallback)

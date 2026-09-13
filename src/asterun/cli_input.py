"""客户端输入边界；stdin 转为既有 text，不给服务端增加读取路径。"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from asterun.errors import AsterunError, INVALID_REQUEST

MAX_STDIN_BYTES = 512 * 1024


def read_stdin() -> str:
    source = getattr(sys.stdin, "buffer", sys.stdin)
    content = source.read(MAX_STDIN_BYTES + 1)
    try:
        raw = content.encode("utf-8", errors="strict") if isinstance(content, str) else content
        if len(raw) > MAX_STDIN_BYTES:
            raise AsterunError(INVALID_REQUEST, "stdin 提示超过 512 KiB 上限")
        return raw.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise AsterunError(INVALID_REQUEST, "stdin 提示必须是严格 UTF-8 文本") from error


def merge_task_input(payload: dict[str, Any], *, text: str | None, input_path: Path | None) -> dict[str, Any]:
    inputs = [name for name in ("text", "path") if name in payload]
    if text is not None:
        inputs.append("--text")
    if input_path is not None:
        inputs.append("--input")
    if len(inputs) > 1:
        raise AsterunError(INVALID_REQUEST, "提示输入只能指定一次：--text、--input、request.text、request.path 互斥",
                           details={"input_sources": inputs})
    payload = dict(payload)
    if text is not None:
        payload["text"] = text
    if input_path is not None:
        if str(input_path) == "-":
            payload["text"] = read_stdin()
        else:
            payload["path"] = str(input_path)
    return payload


def check_ipc_size(method: str, payload: dict[str, Any]) -> None:
    from asterun.service import MAX_MESSAGE

    message = json.dumps({"method": method, "payload": payload, "entry": "cli"},
                         ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
    if len(message) > MAX_MESSAGE:
        raise AsterunError(INVALID_REQUEST, "最终编码请求超过本机 IPC 的 1 MiB 上限；请缩短提示或请求元数据")

"""账户与运行主机绑定。只读环境变量或显式参数，不扫描 HOME 或全局凭据目录。"""

from __future__ import annotations

import os
import socket

from asterun.ids import AccountRef, RuntimeRef

UNBOUND_ACCOUNT = "account:unbound"


def resolve_account_ref(explicit: str | None = None) -> AccountRef:
    if explicit:
        return AccountRef(explicit)
    env = os.environ.get("ASTERUN_ACCOUNT_REF")
    if env:
        return AccountRef(env)
    return AccountRef(UNBOUND_ACCOUNT)


def resolve_runtime_ref(explicit: str | None = None) -> RuntimeRef:
    if explicit:
        return RuntimeRef(explicit)
    env = os.environ.get("ASTERUN_RUNTIME_REF")
    if env:
        return RuntimeRef(env)
    return RuntimeRef(f"host:{socket.gethostname()}")

"""从持久原生失败证据拦截同池后续派发；不猜测余额、不结清未知运行。"""
from asterun.quota_latch import scopes, guard  # noqa: F401

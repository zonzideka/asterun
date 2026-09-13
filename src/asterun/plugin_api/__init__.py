"""供应商无关的插件静态契约；导入本模块不会加载插件或启动进程。"""

from .manifest import PluginManifest
from .protocol import (
    CapabilityProvider,
    PLUGIN_API_MAJOR,
    WORKER_METHODS,
    WORKER_PROTOCOL_VERSION,
    validate_worker_request,
    validate_worker_response,
)

__all__ = [
    "CapabilityProvider", "PluginManifest", "PLUGIN_API_MAJOR", "WORKER_METHODS",
    "WORKER_PROTOCOL_VERSION", "validate_worker_request", "validate_worker_response",
]

"""脱敏诊断。只报告状态、哈希、计数和缺口，不导出凭据或原始后端响应。"""

from __future__ import annotations

import platform
import sys
from typing import Any

from asterun import __version__
from asterun.redact import present_secret_env, redact_mapping, redact_path, redact_ref


def build_diagnose(app) -> dict[str, Any]:
    source = None if app.config.source_path is None else redact_path(app.config.source_path)
    backends = []
    for name in app.config.backends:
        raw = app._backend(name).inspect()
        backends.append(redact_mapping(raw))
    return {
        "package": "asterun",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "config": {
            "source_path": source,
            "schema_version": app.config.schema_version,
            "file_revision": app.config.revision,
            "applied_revision": app.store.get_applied_revision(),
            "workspaces": sorted(app.config.workspaces),
            "default_backend": app.config.default_backend,
            "entries": app.config.entries.to_dict(),
            "workflow_preset": app.config.workflow.preset,
            "scheduler": {
                "max_queue": app.config.scheduler.max_queue,
                "per_backend_concurrency": app.config.scheduler.per_backend_concurrency,
                "tuning_defaults_are_not_measured_slo": True,
            },
        },
        "binding": {
            "account_ref": redact_ref(app.account_ref.value),
            "runtime_ref": redact_ref(app.runtime_ref.value),
        },
        "secret_env": present_secret_env(),
        "backends": backends,
        "scheduler": redact_mapping(app.scheduler.snapshot()),
        "notes": {
            "does_not_include_credentials": True,
            "does_not_start_backends": True,
            "not_a_real_connection_report": True,
            "clean_install_is_not_two_real_user_tasks": True,
        },
        "message": "诊断已脱敏。出现 set 只表示环境变量名存在，不导出值。",
    }

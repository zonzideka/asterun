"""静态、显式、摘要绑定的插件注册；执行器使用前须重新核验。"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import importlib
import os
from pathlib import Path
import re
from typing import Any, Sequence

from asterun.errors import AsterunError, BACKEND_UNAVAILABLE, INVALID_CONFIG
from asterun.plugin_api import PluginManifest


def _invalid(message: str) -> None:
    raise AsterunError(INVALID_CONFIG, message)


def _sha256(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        raise AsterunError(INVALID_CONFIG, "无法核验插件安装文件") from exc


def installation_digest(path: str | Path) -> str:
    """核验文件或完整安装目录；不忽略 pyc，不跟随内容树内符号链接。"""
    path = Path(path)
    if path.is_file():
        return _sha256(path)
    if not path.is_dir():
        _invalid("插件安装路径必须为文件或目录")
    digest = hashlib.sha256(b"asterun-installation-tree/v1\0")
    count = 0
    try:
        for root, directories, files in os.walk(path, followlinks=False):
            directories.sort()
            files.sort()
            for name in directories + files:
                item = Path(root) / name
                count += 1
                if count > 100000 or item.is_symlink():
                    _invalid("插件安装内容树过大或含符号链接")
                relative = item.relative_to(path).as_posix().encode("utf-8")
                digest.update(len(relative).to_bytes(8, "big") + relative)
                if item.is_dir():
                    digest.update(b"D")
                elif item.is_file():
                    digest.update(b"F" + bytes.fromhex(_sha256(item)))
                else:
                    _invalid("插件安装树只允许普通文件和目录")
    except OSError as exc:
        raise AsterunError(INVALID_CONFIG, "无法遍历插件安装内容树") from exc
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PluginRegistration:
    manifest: PluginManifest
    enabled: bool
    source: str
    runner: tuple[str, ...] = ()
    installation_path: Path | None = None
    installation_sha256: str | None = None
    manifest_path: Path | None = None
    manifest_file_sha256: str | None = None
    factory: str | None = None
    runner_sha256: str | None = None
    runtime_path: Path | None = None
    runtime_sha256: str | None = None

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id, "plugin_version": self.manifest.plugin_version,
            "manifest_sha256": self.manifest.sha256, "registered": True,
            "enabled": self.enabled, "source": self.source,
            "runner": list(self.runner), "installation_sha256": self.installation_sha256,
            "runner_sha256": self.runner_sha256, "runtime_sha256": self.runtime_sha256,
            "runtime_integrity_pinned": self.source == "builtin" or self.runtime_path is not None,
            "runtime_integrity_verified": False,
            "installation_evidence_scope": "distribution_artifact_and_explicit_runtime_pins",
            "manifest": self.manifest.to_dict(),
            "authenticated": "not_tested", "callable": "not_tested",
            "resume_verified": False, "native_visible_verified": False,
            "permission_enforcement": "advisory",
            "isolation": "trusted_code_process_isolation_only",
        }


class PluginRegistry:
    """注册是管理员对本地已审阅代码的信任，不是沙箱或供应商认证。"""

    def __init__(self) -> None:
        self._registrations: dict[str, PluginRegistration] = {}

    def _add(self, registration: PluginRegistration) -> PluginRegistration:
        if type(registration.enabled) is not bool:
            _invalid("插件 enabled 必须为布尔值")
        if registration.plugin_id in self._registrations:
            _invalid(f"插件 ID 注册冲突：{registration.plugin_id}")
        self._registrations[registration.plugin_id] = registration
        return registration

    def register_external(self, manifest_path: str | Path, *, runner: Sequence[str],
                          installation_path: str | Path, installation_sha256: str,
                          enabled: bool = False, runtime_path: str | Path | None = None,
                          runtime_sha256: str | None = None) -> PluginRegistration:
        if isinstance(runner, (str, bytes)) or not isinstance(runner, Sequence) or not runner:
            _invalid("插件 runner 必须为显式 argv 数组")
        if any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in runner):
            _invalid("插件 runner argv 无效")
        if not Path(runner[0]).is_absolute():
            _invalid("插件 runner 可执行文件必须为绝对路径")
        runner_sha256 = _sha256(Path(runner[0]))
        if not isinstance(installation_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", installation_sha256):
            _invalid("插件 installation_sha256 必须为 SHA-256 小写十六进制")
        try:
            manifest_path = Path(manifest_path).resolve(strict=True)
            installation_path = Path(installation_path).resolve(strict=True)
        except OSError as exc:
            raise AsterunError(INVALID_CONFIG, "插件 manifest/安装文件不存在") from exc
        if not manifest_path.is_file() or not installation_path.is_file():
            _invalid("插件 manifest/安装文件必须是普通文件")
        before = _sha256(manifest_path)
        manifest = PluginManifest.from_path(manifest_path)
        if before != _sha256(manifest_path):
            _invalid("读取期间插件 manifest 已改变")
        if _sha256(installation_path) != installation_sha256:
            _invalid("插件安装摘要不匹配")
        if (runtime_path is None) != (runtime_sha256 is None):
            _invalid("运行内容路径与摘要必须同时提供")
        if runtime_path is not None:
            runtime_path = Path(runtime_path).absolute()
            if installation_digest(runtime_path) != runtime_sha256:
                _invalid("插件运行内容摘要不匹配")
        return self._add(PluginRegistration(
            manifest=manifest, enabled=enabled, source="external", runner=tuple(runner),
            installation_path=installation_path, installation_sha256=installation_sha256,
            manifest_path=manifest_path, manifest_file_sha256=before,
            runner_sha256=runner_sha256, runtime_path=runtime_path, runtime_sha256=runtime_sha256,
        ))

    def register_builtin(self, manifest: PluginManifest | dict, *, factory: str,
                         enabled: bool = True) -> PluginRegistration:
        if not isinstance(manifest, PluginManifest):
            manifest = PluginManifest.from_dict(manifest)
        if not isinstance(factory, str) or not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", factory):
            _invalid("内置 factory 必须为延迟 module:attribute 入口")
        return self._add(PluginRegistration(manifest=manifest, enabled=enabled, source="builtin", factory=factory))

    def get(self, plugin_id: str) -> PluginRegistration:
        try:
            return self._registrations[plugin_id]
        except (KeyError, TypeError) as exc:
            raise AsterunError(INVALID_CONFIG, "插件尚未显式注册") from exc

    def describe(self, plugin_id: str | None = None) -> dict | list[dict]:
        if plugin_id is not None:
            return self.get(plugin_id).to_dict()
        return [self._registrations[key].to_dict() for key in sorted(self._registrations)]

    def set_enabled(self, plugin_id: str, enabled: bool) -> PluginRegistration:
        if type(enabled) is not bool:
            _invalid("插件 enabled 必须为布尔值")
        registration = replace(self.get(plugin_id), enabled=enabled)
        if enabled:
            self.verify_installation(plugin_id)
        self._registrations[plugin_id] = registration
        return registration

    def verify_installation(self, plugin_id: str) -> PluginRegistration:
        registration = self.get(plugin_id)
        if registration.source == "external":
            if _sha256(Path(registration.runner[0])) != registration.runner_sha256:
                _invalid("插件运行器已改变；需要重新审阅并注册")
            if _sha256(registration.manifest_path) != registration.manifest_file_sha256:
                _invalid("插件 manifest 已改变；需要重新审阅并注册")
            if _sha256(registration.installation_path) != registration.installation_sha256:
                _invalid("插件安装文件已改变；需要重新审阅并注册")
            if registration.runtime_path is not None and installation_digest(registration.runtime_path) != registration.runtime_sha256:
                _invalid("插件运行内容已改变；需要重新审阅并注册")
        return registration

    def load_factory(self, plugin_id: str):
        registration = self.verify_installation(plugin_id)
        if not registration.enabled:
            raise AsterunError(BACKEND_UNAVAILABLE, "插件已停用，不加载执行入口")
        if registration.source != "builtin":
            _invalid("外部插件只能由已注册独立 worker 执行；核心禁止导入")
        module_name, attribute = registration.factory.split(":", 1)
        target = importlib.import_module(module_name)
        for part in attribute.split("."):
            target = getattr(target, part)
        if not callable(target):
            _invalid("内置插件 factory 不可调用")
        return target

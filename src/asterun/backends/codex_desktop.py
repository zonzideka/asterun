"""通过已安装桌面的公开打开入口登记项目；私有投影仅作只读回执。"""

from __future__ import annotations

import json
import hashlib
import math
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
from threading import Lock
import time
from urllib.parse import quote


MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_PLIST_BYTES = 1024 * 1024
POLL_INTERVAL = 0.05
STATE_FILE = ".codex-global-state.json"
PROJECTS_KEY = "local-projects"
MAPPING_KEY = "app-server-project-id-by-legacy-project-id-by-host"


class _Unavailable(Exception):
    def __init__(self, reason, status="unsupported"):
        self.reason, self.status = reason, status


def _identifier(value):
    return (isinstance(value, str) and 0 < len(value) <= 512
            and value == value.strip() and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_bounded(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise _Unavailable("desktop_state_unreadable")
        data = source.read(limit + 1)
        if len(data) > limit:
            raise _Unavailable("desktop_state_unreadable")
        return data


class CodexDesktop:
    # 同进程串行观察；跨进程未决意图另持久化，避免未知结果后重发。
    _registration_lock = Lock()

    def __init__(self, binary, native_home):
        self.binary = binary
        self.native_home = native_home

    @staticmethod
    def _deadline(timeout_seconds):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or timeout_seconds <= 0):
            raise _Unavailable("invalid_presentation_timeout")
        return time.monotonic() + min(timeout_seconds, 60)

    @staticmethod
    def _path(value, *, directory=False):
        if not isinstance(value, (str, Path)) or not str(value) or "\x00" in str(value):
            raise _Unavailable("invalid_presentation_path")
        path = Path(value)
        if not path.is_absolute():
            raise _Unavailable("absolute_path_required")
        path = path.resolve()
        if directory and not path.is_dir():
            raise _Unavailable("project_directory_required")
        return path

    def _candidates(self):
        return [base / name for base in (Path("/Applications"), Path.home() / "Applications")
                for name in ("ChatGPT.app", "Codex.app")]

    @staticmethod
    def _valid_bundle(bundle):
        try:
            data = plistlib.loads(_read_bounded(bundle / "Contents/Info.plist", MAX_PLIST_BYTES))
            if not isinstance(data, dict) or data.get("CFBundleIdentifier") != "com.openai.codex":
                return False
            executable = data.get("CFBundleExecutable")
            if (not _identifier(executable) or Path(executable).name != executable
                    or not (bundle / "Contents/MacOS" / executable).is_file()):
                return False
            urls = data.get("CFBundleURLTypes")
            return isinstance(urls, list) and any(
                isinstance(item, dict) and isinstance(item.get("CFBundleURLSchemes"), list)
                and "codex" in item["CFBundleURLSchemes"] for item in urls)
        except Exception:
            return False

    def _bundle(self):
        if sys.platform != "darwin":
            raise _Unavailable("desktop_platform_unsupported")
        binary = self._path(self.binary)
        if not binary.is_file():
            raise _Unavailable("desktop_binary_unavailable")
        ancestor = next((parent for parent in binary.parents if parent.suffix == ".app"), None)
        if ancestor is not None:
            if not self._valid_bundle(ancestor):
                raise _Unavailable("desktop_bundle_unavailable")
            return ancestor
        candidates = {path.resolve() for path in self._candidates() if self._valid_bundle(path)}
        if len(candidates) > 1:
            raise _Unavailable("ambiguous_desktop_bundles", "conflict")
        if not candidates:
            raise _Unavailable("desktop_bundle_unavailable")
        return candidates.pop()

    def _state(self, home):
        try:
            raw = json.loads(_read_bounded(home / STATE_FILE, MAX_STATE_BYTES), object_pairs_hook=_unique_object)
        except FileNotFoundError:
            raw = {}
        except _Unavailable:
            raise
        except Exception:
            raise _Unavailable("desktop_state_unreadable") from None
        if not isinstance(raw, dict):
            raise _Unavailable("desktop_state_schema_unknown")
        projects, mappings = raw.get(PROJECTS_KEY, {}), raw.get(MAPPING_KEY, {})
        if not isinstance(projects, dict) or not isinstance(mappings, dict):
            raise _Unavailable("desktop_state_schema_unknown")
        key = "local:" + str(home)
        mapping = mappings.get(key)
        default_home = (Path.home() / ".codex").resolve()
        if mapping is None and (home != default_home or mappings):
            raise _Unavailable("desktop_home_mismatch", "conflict")
        if mapping is not None and (not isinstance(mapping, dict)
                or any(not _identifier(k) or not _identifier(v) for k, v in mapping.items())):
            raise _Unavailable("desktop_state_schema_unknown")
        if home != default_home and not mapping:
            raise _Unavailable("desktop_home_unconfirmed", "conflict")
        return projects, mapping or {}

    def _registered(self, home, root):
        projects, mapping = self._state(home)
        matches = []
        for legacy_id, project in projects.items():
            if (not _identifier(legacy_id) or not isinstance(project, dict) or project.get("id") != legacy_id
                    or not isinstance(project.get("rootPaths"), list) or not project["rootPaths"]):
                raise _Unavailable("desktop_state_schema_unknown")
            roots = [self._path(path) for path in project["rootPaths"]]
            if root in roots:
                if roots != [root]:
                    raise _Unavailable("desktop_project_roots_conflict", "conflict")
                matches.append(legacy_id)
        if len(matches) > 1:
            raise _Unavailable("ambiguous_desktop_projects", "conflict")
        if not matches:
            return None, False
        legacy_id = matches[0]
        native_id = mapping.get(legacy_id)
        if native_id is None:
            return None, True
        if sum(value == native_id for value in mapping.values()) != 1:
            raise _Unavailable("ambiguous_desktop_project_mapping", "conflict")
        return {"registration": "registered", "project_id": native_id,
                "desktop_project_id": legacy_id, "project_root": str(root)}, True

    @staticmethod
    def _intent_directory():
        # 多个 Asterun 实例可共享同一桌面，因此意图也按本机用户共享。
        # 这是 Asterun 自有记录，不写入 CODEX_HOME 或桌面状态。
        return Path.home() / ".local/share/asterun/codex-desktop-intents"

    @staticmethod
    def _intent_key(home, root):
        return hashlib.sha256((str(home) + "\x00" + str(root)).encode()).hexdigest() + ".pending"

    @classmethod
    def _intent_directory_fd(cls, *, create):
        directory = cls._intent_directory()
        if create:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            # 另一进程可能刚 mkdir 而尚未同步，不能以目录已存在推断已落盘。
            for ancestor in directory.parents:
                parent = os.open(ancestor, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
                if ancestor == Path.home():
                    break
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(descriptor)
            raise _Unavailable("desktop_intent_unavailable", "unknown")
        return descriptor

    def _claim_registration(self, home, root):
        directory = None
        try:
            directory = self._intent_directory_fd(create=True)
            key = self._intent_key(home, root)
            try:
                descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory)
            except FileExistsError:
                info = os.stat(key, dir_fd=directory, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise _Unavailable("desktop_intent_unavailable", "unknown")
                return False
            try:
                os.fsync(descriptor)
                os.fsync(directory)
            finally:
                os.close(descriptor)
            return True
        except Exception:
            # 即使落盘部分完成也保留意图，不能把不确定请求当成未发送。
            raise _Unavailable("desktop_intent_unavailable", "unknown") from None
        finally:
            if directory is not None:
                os.close(directory)

    def _clear_registration(self, home, root):
        directory = None
        try:
            directory = self._intent_directory_fd(create=False)
            os.unlink(self._intent_key(home, root), dir_fd=directory)
            os.fsync(directory)
        except Exception:
            # 已确认的桌面登记仍可使用；遗留意图只会抑制未来不确定的重发。
            pass
        finally:
            if directory is not None:
                os.close(directory)

    @staticmethod
    def _open(bundle, target, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _Unavailable("desktop_open_unconfirmed", "unknown")
        try:
            result = subprocess.run(["/usr/bin/open", "-g", "-a", str(bundle), target],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}, timeout=remaining,
                check=False)
        except subprocess.TimeoutExpired:
            raise _Unavailable("desktop_open_unconfirmed", "unknown") from None
        except Exception:
            raise _Unavailable("desktop_open_failed", "unknown") from None
        if result.returncode != 0:
            raise _Unavailable("desktop_open_failed", "unknown")

    def register_project(self, root, timeout_seconds):
        acquired = False
        try:
            deadline = self._deadline(timeout_seconds)
            acquired = self._registration_lock.acquire(timeout=max(0, deadline - time.monotonic()))
            if not acquired:
                raise _Unavailable("desktop_registration_busy", "unknown")
            bundle = self._bundle()
            root, home = self._path(root, directory=True), self._path(self.native_home)
            registered, exists = self._registered(home, root)
            if registered is not None:
                self._clear_registration(home, root)
                return registered
            if not exists and self._claim_registration(home, root):
                # 声明意图期间桌面可能已由另一调用方登记，再回读一次。
                registered, exists = self._registered(home, root)
                if registered is not None:
                    self._clear_registration(home, root)
                    return registered
                if not exists:
                    self._open(bundle, str(root), deadline)
            while time.monotonic() < deadline:
                registered, _ = self._registered(home, root)
                if registered is not None:
                    self._clear_registration(home, root)
                    return registered
                time.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
            raise _Unavailable("desktop_registration_unconfirmed", "unknown")
        except _Unavailable as exc:
            return {"registration": exc.status, "reason": exc.reason}
        except Exception:
            return {"registration": "unknown", "reason": "desktop_presentation_failed"}
        finally:
            if acquired:
                self._registration_lock.release()

    def open_thread(self, thread_id, timeout_seconds=2):
        try:
            deadline = self._deadline(timeout_seconds)
            if not _identifier(thread_id):
                raise _Unavailable("invalid_thread_id")
            bundle = self._bundle()
            self._state(self._path(self.native_home))
            self._open(bundle, "codex://threads/" + quote(thread_id, safe=""), deadline)
            return {"status": "requested", "thread_id": thread_id}
        except _Unavailable as exc:
            return {"status": exc.status, "reason": exc.reason}
        except Exception:
            return {"status": "unknown", "reason": "desktop_presentation_failed"}

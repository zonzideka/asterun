from __future__ import annotations

import hashlib
import json
from pathlib import Path
import plistlib
import subprocess
import sys
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from asterun.backends import codex_desktop as desktop
from asterun.backends.codex_desktop import CodexDesktop


def bundle(path, *, identifier="com.openai.codex", schemes=None):
    resources = path / "Contents/Resources"
    resources.mkdir(parents=True)
    executable = path / "Contents/MacOS/ChatGPT"
    executable.parent.mkdir()
    executable.write_text("offline fixture")
    binary = resources / "codex"
    binary.write_text("offline fixture")
    (path / "Contents/Info.plist").write_bytes(plistlib.dumps({
        "CFBundleIdentifier": identifier, "CFBundleExecutable": "ChatGPT",
        "CFBundleURLTypes": [{"CFBundleURLSchemes": ["codex"] if schemes is None else schemes}],
    }))
    return binary


@pytest.fixture
def fixture(isolated_env, monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    binary = bundle(isolated_env / "Fixed ChatGPT.app")
    root = isolated_env / "repository with spaces"
    root.mkdir()
    home = Path.home() / ".codex"
    calls = []
    def launch(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(desktop.subprocess, "run", launch)
    return SimpleNamespace(binary=binary, root=root.resolve(), home=home.resolve(), calls=calls,
                           bridge=CodexDesktop(binary, home))


def state(fixture, *, projects=None, mapping=None, home=None):
    home = home or fixture.home
    home.mkdir(parents=True, exist_ok=True)
    project = {"id": "ui-id", "name": "Project", "rootPaths": [str(fixture.root)],
               "createdAt": 1, "updatedAt": 1}
    raw = {"local-projects": {"ui-id": project} if projects is None else projects,
           "app-server-project-id-by-legacy-project-id-by-host": {
               "local:" + str(home): {"ui-id": "native-id"} if mapping is None else mapping}}
    (home / desktop.STATE_FILE).write_text(json.dumps(raw))
    return raw


def test_existing_registration_reused_without_open_and_file_unchanged(fixture):
    state(fixture)
    before = (fixture.home / desktop.STATE_FILE).read_bytes()
    result = fixture.bridge.register_project(fixture.root, 1)
    assert result == {"registration": "registered", "project_id": "native-id",
                      "desktop_project_id": "ui-id", "project_root": str(fixture.root)}
    assert fixture.calls == []
    assert (fixture.home / desktop.STATE_FILE).read_bytes() == before


def test_first_project_opens_fixed_bundle_once_and_reads_ui_mapping(fixture, monkeypatch):
    def launch(args, **kwargs):
        fixture.calls.append((args, kwargs))
        state(fixture)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(desktop.subprocess, "run", launch)
    assert not fixture.home.exists()
    result = fixture.bridge.register_project(fixture.root, 1)
    assert result["registration"] == "registered"
    assert len(fixture.calls) == 1
    args, kwargs = fixture.calls[0]
    assert args == ["/usr/bin/open", "-g", "-a", str(fixture.binary.parents[2]), str(fixture.root)]
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
    assert "shell" not in kwargs
    assert set(kwargs["env"]) == {"HOME", "PATH"}
    assert 0 < kwargs["timeout"] <= 1


def test_existing_ui_project_waits_for_late_mapping_without_open(fixture, monkeypatch):
    state(fixture, mapping={})
    sleeps = []
    def later(seconds):
        sleeps.append(seconds)
        state(fixture)
    monkeypatch.setattr(desktop.time, "sleep", later)
    assert fixture.bridge.register_project(fixture.root, 1)["project_id"] == "native-id"
    assert sleeps and fixture.calls == []


def test_open_timeout_is_unknown_and_late_receipt_prevents_second_open(fixture, monkeypatch):
    def timeout(args, **kwargs):
        fixture.calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"], output="secret-marker")
    monkeypatch.setattr(desktop.subprocess, "run", timeout)
    first = fixture.bridge.register_project(fixture.root, 1)
    assert first == {"registration": "unknown", "reason": "desktop_open_unconfirmed"}
    state(fixture)
    assert fixture.bridge.register_project(fixture.root, 1)["registration"] == "registered"
    assert len(fixture.calls) == 1
    assert "secret-marker" not in json.dumps(first)


def test_missing_receipt_times_out_without_reopening(fixture):
    result = fixture.bridge.register_project(fixture.root, 0.02)
    assert result == {"registration": "unknown", "reason": "desktop_registration_unconfirmed"}
    assert len(fixture.calls) == 1
    assert not fixture.home.exists()


@pytest.mark.parametrize("change,reason", [
    ("duplicate", "ambiguous_desktop_projects"),
    ("multiple_roots", "desktop_project_roots_conflict"),
    ("mapping_alias", "ambiguous_desktop_project_mapping"),
    ("wrong_id", "desktop_state_schema_unknown"),
    ("invalid_roots", "desktop_state_schema_unknown"),
    ("mapping_type", "desktop_state_schema_unknown"),
    ("other_home", "desktop_home_mismatch"),
])
def test_ambiguous_or_invalid_state_never_opens(fixture, change, reason):
    raw = state(fixture)
    projects = raw[desktop.PROJECTS_KEY]
    mapping = raw[desktop.MAPPING_KEY]["local:" + str(fixture.home)]
    if change == "duplicate":
        projects["other-ui"] = projects["ui-id"] | {"id": "other-ui"}
    elif change == "multiple_roots": projects["ui-id"]["rootPaths"].append(str(fixture.home))
    elif change == "mapping_alias": mapping["other-ui"] = "native-id"
    elif change == "wrong_id": projects["ui-id"]["id"] = "different"
    elif change == "invalid_roots": projects["ui-id"]["rootPaths"] = None
    elif change == "mapping_type": mapping["ui-id"] = 1
    elif change == "other_home": raw[desktop.MAPPING_KEY] = {"local:/different-home": mapping}
    (fixture.home / desktop.STATE_FILE).write_text(json.dumps(raw))
    assert fixture.bridge.register_project(fixture.root, 1)["reason"] == reason
    assert fixture.calls == []


def test_custom_home_requires_existing_exact_host_mapping(fixture):
    custom = fixture.home.parent / "custom-home"
    bridge = CodexDesktop(fixture.binary, custom)
    assert bridge.register_project(fixture.root, 1)["reason"] == "desktop_home_mismatch"
    state(fixture, home=custom)
    assert bridge.register_project(fixture.root, 1)["project_id"] == "native-id"
    assert fixture.calls == []


def test_custom_home_empty_mapping_does_not_confirm_desktop_identity(fixture):
    custom = fixture.home.parent / "custom-home"
    state(fixture, home=custom, mapping={})
    assert CodexDesktop(fixture.binary, custom).register_project(fixture.root, 1)["reason"] == "desktop_home_unconfirmed"
    assert fixture.calls == []


@pytest.mark.parametrize("contents", [b"[]", b"not-json", b'{"local-projects": [], "secret": "private-marker"}',
                                      b'{"local-projects": {}, "local-projects": {}}'])
def test_invalid_file_is_bounded_and_never_echoed(fixture, contents):
    fixture.home.mkdir()
    (fixture.home / desktop.STATE_FILE).write_bytes(contents)
    result = fixture.bridge.register_project(fixture.root, 1)
    assert result["registration"] != "registered"
    assert "private-marker" not in json.dumps(result)
    assert fixture.calls == []


def test_oversized_file_and_symlink_rejected(fixture, monkeypatch):
    state(fixture)
    monkeypatch.setattr(desktop, "MAX_STATE_BYTES", 16)
    assert fixture.bridge.register_project(fixture.root, 1)["reason"] == "desktop_state_unreadable"
    path = fixture.home / desktop.STATE_FILE
    path.unlink()
    path.symlink_to(fixture.binary)
    assert fixture.bridge.register_project(fixture.root, 1)["reason"] == "desktop_state_unreadable"
    assert fixture.calls == []


@pytest.mark.parametrize("problem", ["identifier", "scheme", "missing", "platform"])
def test_unavailable_bundle_never_runs_installer_or_open(fixture, monkeypatch, problem):
    info = fixture.binary.parents[1] / "Info.plist"
    if problem == "missing": info.unlink()
    elif problem == "platform": monkeypatch.setattr(desktop.sys, "platform", "linux")
    else:
        raw = plistlib.loads(info.read_bytes())
        if problem == "identifier": raw["CFBundleIdentifier"] = "example.other"
        else: raw["CFBundleURLTypes"] = [{"CFBundleURLSchemes": ["other"]}]
        info.write_bytes(plistlib.dumps(raw))
    assert fixture.bridge.register_project(fixture.root, 1)["registration"] == "unsupported"
    assert fixture.calls == []


def test_external_binary_only_uses_unique_valid_standard_candidate(fixture, monkeypatch):
    binary = fixture.root / "cli"
    binary.write_text("offline")
    bridge = CodexDesktop(binary, fixture.home)
    first = fixture.binary.parents[2]
    second = bundle(fixture.root / "Other.app").parents[2]
    monkeypatch.setattr(bridge, "_candidates", lambda: [first, second])
    assert bridge.register_project(fixture.root, 1)["reason"] == "ambiguous_desktop_bundles"
    assert fixture.calls == []
    monkeypatch.setattr(bridge, "_candidates", lambda: [first])
    state(fixture)
    assert bridge.register_project(fixture.root, 1)["registration"] == "registered"


@pytest.mark.parametrize("value", [None, True, False, 0, -1, "1", float("nan"), float("inf")])
def test_invalid_timeout_does_not_open(fixture, value):
    assert fixture.bridge.register_project(fixture.root, value)["reason"] == "invalid_presentation_timeout"
    assert fixture.bridge.open_thread("thread", value)["reason"] == "invalid_presentation_timeout"
    assert fixture.calls == []


def test_existing_thread_url_is_encoded_and_only_requested(fixture):
    result = fixture.bridge.open_thread("thread/part?x=#fragment")
    assert result == {"status": "requested", "thread_id": "thread/part?x=#fragment"}
    assert fixture.calls[0][0][-1] == "codex://threads/thread%2Fpart%3Fx%3D%23fragment"
    assert "verified" not in result


@pytest.mark.parametrize("thread_id", [None, "", " line", "line\nvalue", "x" * 513])
def test_invalid_thread_id_never_opens(fixture, thread_id):
    assert fixture.bridge.open_thread(thread_id)["reason"] == "invalid_thread_id"
    assert fixture.calls == []


def test_thread_open_errors_return_only_sanitized_presentation_result(fixture, monkeypatch):
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert fixture.bridge.open_thread("thread") == {"status": "unknown", "reason": "desktop_open_failed"}
    def timeout(*args, **kwargs): raise subprocess.TimeoutExpired("open", 2, stderr="private-marker")
    monkeypatch.setattr(desktop.subprocess, "run", timeout)
    assert fixture.bridge.open_thread("thread") == {"status": "unknown", "reason": "desktop_open_unconfirmed"}


def test_concurrent_registrations_share_serialized_readback(fixture, monkeypatch):
    entered, release = Event(), Event()
    def launch(args, **kwargs):
        fixture.calls.append((args, kwargs))
        entered.set()
        assert release.wait(2)
        state(fixture)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(desktop.subprocess, "run", launch)
    results = []
    first = Thread(target=lambda: results.append(fixture.bridge.register_project(fixture.root, 2)))
    second_bridge = CodexDesktop(fixture.binary, fixture.home)
    second = Thread(target=lambda: results.append(second_bridge.register_project(fixture.root, 2)))
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(3)
    second.join(3)
    assert len(results) == 2 and all(item["registration"] == "registered" for item in results)
    assert len(fixture.calls) == 1


def intent_path(fixture):
    key = hashlib.sha256((str(fixture.home.resolve()) + "\0" + str(fixture.root.resolve())).encode()).hexdigest()
    return Path.home() / ".local/share/asterun/codex-desktop-intents" / (key + ".pending")


def test_unknown_receipt_across_new_bridge_only_opens_once(fixture):
    first = fixture.bridge.register_project(fixture.root, 0.02)
    assert first["registration"] == "unknown"
    assert intent_path(fixture).is_file()
    assert intent_path(fixture).read_bytes() == b""
    second = CodexDesktop(fixture.binary, fixture.home).register_project(fixture.root, 0.02)
    assert second["registration"] == "unknown"
    assert len(fixture.calls) == 1
    assert not fixture.home.exists()


def test_timed_out_open_without_receipt_does_not_repeat_across_instances(fixture, monkeypatch):
    def timeout(args, **kwargs):
        fixture.calls.append((args, kwargs))
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])
    monkeypatch.setattr(desktop.subprocess, "run", timeout)
    assert fixture.bridge.register_project(fixture.root, 0.02)["reason"] == "desktop_open_unconfirmed"
    assert intent_path(fixture).is_file()
    assert CodexDesktop(fixture.binary, fixture.home).register_project(fixture.root, 0.02)["registration"] == "unknown"
    assert len(fixture.calls) == 1


def test_late_receipt_clears_intent_and_later_user_removal_allows_registration(fixture, monkeypatch):
    assert fixture.bridge.register_project(fixture.root, 0.02)["registration"] == "unknown"
    pending = intent_path(fixture)
    assert pending.is_file()
    state(fixture)
    assert CodexDesktop(fixture.binary, fixture.home).register_project(fixture.root, 1)["registration"] == "registered"
    assert not pending.exists()
    assert len(fixture.calls) == 1
    state(fixture, projects={}, mapping={})
    def recreated(args, **kwargs):
        fixture.calls.append((args, kwargs))
        state(fixture)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(desktop.subprocess, "run", recreated)
    assert CodexDesktop(fixture.binary, fixture.home).register_project(fixture.root, 1)["registration"] == "registered"
    assert len(fixture.calls) == 2 and not pending.exists()


def test_exclusive_intent_claim_has_one_winner_across_processes(fixture):
    script = """
from pathlib import Path
import sys
from asterun.backends.codex_desktop import CodexDesktop
bridge = CodexDesktop(sys.argv[1], Path(sys.argv[2]))
print('claimed' if bridge._claim_registration(Path(sys.argv[2]), Path(sys.argv[3])) else 'existing')
"""
    environment = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin",
                   "PYTHONPATH": str(Path(desktop.__file__).resolve().parents[2]),
                   "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    command = [sys.executable, "-c", script, str(fixture.binary), str(fixture.home), str(fixture.root)]
    processes = [subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    try:
        outputs = []
        for process in processes:
            output, error = process.communicate(timeout=10)
            assert process.returncode == 0, error
            outputs.append(output.strip())
        assert outputs.count("claimed") == 1
        assert outputs.count("existing") == 3
        assert intent_path(fixture).is_file()
        assert intent_path(fixture).read_bytes() == b""
        assert fixture.calls == []
        assert not fixture.home.exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_claim_syncs_parent_chain_when_another_creator_has_not_synced(fixture, monkeypatch):
    # 两个 claim 直接并发，模拟不同进程不共享 register_project 的内存锁。
    # A 停在 mkdir 完成后；B 必须自行同步新目录父项，不能依赖 A 恢复。
    directory = intent_path(fixture).parent
    created, release = Event(), Event()
    original_mkdir, original_sync = Path.mkdir, desktop.os.fsync
    syncs, results, errors = [], {}, []

    def mkdir(path, *args, **kwargs):
        result = original_mkdir(path, *args, **kwargs)
        if path == directory and current_thread().name == "intent-creator":
            created.set()
            assert release.wait(5)
        return result

    def sync(descriptor):
        info = desktop.os.fstat(descriptor)
        original_sync(descriptor)
        syncs.append((current_thread().name, info.st_dev, info.st_ino))

    def creator():
        try:
            results["creator"] = fixture.bridge._claim_registration(fixture.home, fixture.root)
        except Exception as error:
            errors.append(error)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(desktop.os, "fsync", sync)
    first = Thread(target=creator, name="intent-creator")
    first.start()
    try:
        assert created.wait(5)
        second = CodexDesktop(fixture.binary, fixture.home)
        assert second._claim_registration(fixture.home, fixture.root) is True
        assert not release.is_set() and first.is_alive()
        completed = {(device, inode) for name, device, inode in syncs
                     if name == current_thread().name}
        for parent in directory.parents:
            info = parent.stat()
            assert (info.st_dev, info.st_ino) in completed, "claim 返回前未同步必要父目录"
            if parent == Path.home():
                break
        for path in (directory, intent_path(fixture)):
            info = path.stat()
            assert (info.st_dev, info.st_ino) in completed
    finally:
        release.set()
        first.join(5)
    assert not first.is_alive() and not errors
    assert results == {"creator": False}
    assert fixture.calls == []


@pytest.mark.parametrize("problem", ["directory_symlink", "directory_file", "file_symlink", "file_directory", "file_nonempty"])
def test_invalid_intent_path_never_opens_desktop(fixture, problem):
    pending = intent_path(fixture)
    parent = pending.parent
    parent.parent.mkdir(parents=True, exist_ok=True)
    if problem == "directory_symlink":
        other = fixture.root / "intent-target"
        other.mkdir()
        parent.symlink_to(other, target_is_directory=True)
    elif problem == "directory_file": parent.write_bytes(b"wrong-type")
    else:
        parent.mkdir(mode=0o700)
        if problem == "file_symlink": pending.symlink_to(fixture.binary)
        elif problem == "file_directory": pending.mkdir()
        else:
            pending.write_bytes(b"invalid-marker")
            pending.chmod(0o600)
    result = fixture.bridge.register_project(fixture.root, 0.02)
    assert result["registration"] != "registered"
    assert result.get("reason")
    assert fixture.calls == []
    assert not fixture.home.exists()


@pytest.mark.parametrize("failure", ["directory", "fsync"])
def test_intent_persistence_failure_prevents_any_open(fixture, monkeypatch, failure):
    def denied(*args, **kwargs):
        raise OSError("private-marker")
    if failure == "directory": monkeypatch.setattr(CodexDesktop, "_intent_directory", staticmethod(denied))
    else: monkeypatch.setattr(desktop.os, "fsync", denied)
    result = fixture.bridge.register_project(fixture.root, 0.02)
    assert result["registration"] != "registered"
    assert "private-marker" not in json.dumps(result)
    assert fixture.calls == []
    assert not fixture.home.exists()

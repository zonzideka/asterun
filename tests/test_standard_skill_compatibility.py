"""兼容门禁只观察临时 CLI 替身；不启动真实后端或接触生产实例。"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import stat
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "asterun_skill_compatibility", ROOT / "integrations/skills/asterun/scripts/run.py")
skill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skill)


@pytest.fixture
def fake_cli(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"cli": "0.1.0a13", "core": "0.1.0a13"}))
    log = tmp_path / "calls.jsonl"
    binary = tmp_path / "asterun"
    binary.write_text(f"#!{sys.executable}\n" + f"settings_path={str(settings)!r}\nlog_path={str(log)!r}\n" + '''
import json, pathlib, sys, time
config = json.loads(pathlib.Path(settings_path).read_text())
args = sys.argv[1:]
command = args[0] if args[0] != '--state-dir' else args[3]
with open(log_path, 'a') as handle:
    handle.write(json.dumps(args) + '\\n')
if '--help' in args:
    if config.get('help_timeout'):
        time.sleep(30)
    if not config.get('cli_features', True):
        print('usage: asterun ' + command)
        raise SystemExit(2 if command == 'workflow-snapshot' else 0)
    print('usage: asterun ' + command + ' [--compact] [--no-events]')
    raise SystemExit(0)
if command in ('version', 'diagnose'):
    print(json.dumps({'ok': True, 'data': {'version': config['cli' if command == 'version' else 'core']}}))
    raise SystemExit(0)
task_id = args[4] if len(args) > 4 else ''
if task_id.startswith('tsk_skill_probe_'):
    mode = config.get('probe', 'supported')
    if mode == 'timeout':
        time.sleep(30)
    if mode == 'malformed':
        print('not json')
        raise SystemExit(1)
    code = {'unsupported': 'INVALID_REQUEST', 'unreachable': 'BACKEND_UNAVAILABLE'}.get(mode, 'NOT_FOUND')
    details = {'id': 'another_task' if mode == 'wrong_id' else task_id}
    print(json.dumps({'ok': mode == 'success', 'error': {'code': code, 'details': details}}))
    raise SystemExit(0 if mode == 'zero_exit' else 1)
print(json.dumps({'ok': True, 'data': {'task': {'id': 'tsk_created'}, 'run': {'status': 'queued'}}, 'ids': {'task_id': 'tsk_created'}}))
''')
    binary.chmod(0o700)

    def configure(**options):
        settings.write_text(json.dumps({"cli": "0.1.0a13", "core": "0.1.0a13", **options}))

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def invoke(timeout="20"):
        args = skill.parser().parse_args([
            "--asterun-bin", str(binary), "--state-dir", str(tmp_path / "state"),
            "--artifacts-dir", str(tmp_path / "artifacts"), "--cli-timeout", timeout,
            "submit", "--workspace", "demo", "--backend", "fake", "--input", "task.md",
            "--idempotency-key", "compatibility-test"])
        return skill.execute(args)

    return configure, calls, invoke


@pytest.mark.parametrize("cli,core", [("0.1.0a14", "0.1.0a14"), ("0.1.0a13", "0.1.0a14"),
                                      ("0.1.0a14", "0.1.0a13"), ("0.1.0a13", "0.1.0a13")])
def test_compatible_versions_or_verified_historical_features(fake_cli, cli, core):
    configure, calls, invoke = fake_cli
    configure(cli=cli, core=core)
    result, code = invoke()
    assert code == 0 and result["ok"]
    recorded = calls()
    assert len([args for args in recorded if "task-submit" in args]) == 1
    assert len([args for args in recorded if "--help" in args]) == (3 if "0.1.0a13" in (cli, core) else 0)
    probes = [args for args in recorded if any(arg.startswith("tsk_skill_probe_") for arg in args)]
    assert len(probes) == (2 if core == "0.1.0a13" else 0)
    directory = Path(result["artifacts"]["directory"])
    evidence = json.loads((directory / "compatibility.json").read_text())
    assert evidence["cli_version"] == cli and evidence["core_version"] == core
    assert evidence["cached"] is False
    assert evidence["mode"] == ("historical_a13_capabilities" if "0.1.0a13" in (cli, core) else "release_version")
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())


def test_published_a13_cli_stops_before_submit_and_server_probe(fake_cli):
    configure, calls, invoke = fake_cli
    configure(cli_features=False)
    result, code = invoke()
    assert code == 2 and result["error"]["code"] == "CORE_VERSION_UNSUPPORTED"
    assert not any("task-submit" in args or "diagnose" in args for args in calls())


@pytest.mark.parametrize("mode,expected", [("unsupported", "CORE_VERSION_UNSUPPORTED"),
    ("unreachable", "CORE_UNREACHABLE"), ("malformed", "CORE_CAPABILITY_UNKNOWN"),
    ("wrong_id", "CORE_CAPABILITY_UNKNOWN"), ("success", "CORE_CAPABILITY_UNKNOWN"),
    ("zero_exit", "CORE_CAPABILITY_UNKNOWN")])
def test_unverified_historical_core_never_receives_submission(fake_cli, mode, expected):
    configure, calls, invoke = fake_cli
    configure(cli="0.1.0a14", probe=mode)
    result, code = invoke()
    assert code != 0 and result["error"]["code"] == expected
    assert not any("task-submit" in args for args in calls())
    if expected != "CORE_VERSION_UNSUPPORTED":
        assert "current_version" not in result


@pytest.mark.parametrize("side", ["cli", "core"])
@pytest.mark.parametrize("version", ["0.1.0a12", "0.1.0a13+source", "0.1.0a13.dev1"])
def test_only_exact_a13_is_eligible_for_historical_detection(fake_cli, side, version):
    configure, calls, invoke = fake_cli
    configure(**{"cli": "0.1.0a14", "core": "0.1.0a14", side: version})
    result, code = invoke()
    assert code == 2 and result["error"]["code"] == "CORE_VERSION_UNSUPPORTED"
    assert not any("task-submit" in args or "--help" in args for args in calls())


@pytest.mark.parametrize("side,expected", [("cli", "INVALID_CLI_RESPONSE"), ("core", "CORE_VERSION_UNKNOWN")])
def test_malformed_version_is_unknown_not_an_upgrade_instruction(fake_cli, side, expected):
    configure, calls, invoke = fake_cli
    configure(**{"cli": "0.1.0a14", "core": "0.1.0a14", side: "garbage"})
    result, code = invoke()
    assert code != 0 and result["error"]["code"] == expected
    assert "current_version" not in result
    assert not any("task-submit" in args for args in calls())


def test_restarted_core_is_rechecked_even_in_same_skill_process(fake_cli):
    configure, calls, invoke = fake_cli
    configure(cli="0.1.0a14", core="0.1.0a14")
    first, code = invoke()
    assert code == 0 and first["ok"]
    configure(cli="0.1.0a14", core="0.1.0a13", probe="unsupported")
    second, code = invoke()
    assert code != 0 and second["error"]["code"] == "CORE_VERSION_UNSUPPORTED"
    assert len([args for args in calls() if "diagnose" in args]) == 2
    assert len([args for args in calls() if "task-submit" in args]) == 1


@pytest.mark.parametrize("options,expected", [({"help_timeout": True}, "CLIENT_TIMEOUT"),
                                             ({"probe": "timeout"}, "CORE_UNREACHABLE")])
def test_capability_probe_timeout_preserves_unknown_and_prevents_submit(fake_cli, options, expected):
    configure, calls, invoke = fake_cli
    configure(**options)
    result, code = invoke("1")
    assert code == 124 and result["error"]["code"] == expected
    assert not any("task-submit" in args for args in calls())
    assert "current_version" not in result

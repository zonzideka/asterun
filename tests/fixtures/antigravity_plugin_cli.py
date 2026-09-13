"""独立包 CLI wrapper 模板；只运行原离线 fixture，不查找或执行真实 agy。"""
import json
import os
from pathlib import Path
import sys
import uuid

# 测试安装时将三个字面量机械替换为临时 fixture 的绝对路径与模式。
FIXTURE_PATH = "__FIXTURE_PATH__"
MODE = "__MODE__"
MARKER_PATH = "__MARKER_PATH__"

marker = Path(MARKER_PATH)
if sys.argv[1:] == ["--version"]:
    with marker.open("a") as handle:
        handle.write("version\n")
    print("1.2.0")
    raise SystemExit(0)

expected = ["--input-format", "stream-json", "--output-format", "stream-json", "--model",
            "fixture-model", "--disable-slash-commands", "--print-timeout", "5m", "--add-dir", os.getcwd()]
assert sys.argv[1:] == expected
assert not any(key in os.environ for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTIGRAVITY_PERM_GRANTS", "AGY_ADC_AUTH", "PYTHONPATH"))
settings = json.loads((Path(os.environ["HOME"]) / ".gemini/antigravity-cli/settings.json").read_text())
assert settings.get("toolPermission", "request-review") == "request-review"
assert settings.get("useG1Credits", False) is False
assert settings["permissions"]["allow"] == [f"read_file({os.getcwd()})"]
assert settings["permissions"]["deny"] == ["write_file(*)", "read_url(*)", "execute_url(*)", "command(*)", "unsandboxed(*)", "mcp(*)"]
with marker.open("a") as handle:
    handle.write("dispatch\n")
if MODE == "quota":
    value = json.loads(sys.stdin.read())
    assert value["event"] == "user"
    session = "quota-fixture-session"
    print(json.dumps({"event": "init", "conversation_id": session,
        "init": {"cwd": os.getcwd(), "permission_mode": "request-review", "model": "fixture-model"}}), flush=True)
    print(json.dumps({"event": "result", "result": {"conversation_id": session, "status": "ERROR",
        "response": "", "error": "quota exhausted private diagnostic"}}), flush=True)
    raise SystemExit(0)
if MODE == "cumulative":
    os.environ["AGY_FIXTURE_MODE"] = "happy"
else:
    os.environ["AGY_FIXTURE_MODE"] = MODE
os.environ["AGY_FIXTURE_READY"] = str(marker.with_suffix(".pid"))
sys.argv = [FIXTURE_PATH]
source = Path(FIXTURE_PATH).read_text()
source = source.replace('SESSION = "fixture-conversation-123"', f'SESSION = {str(uuid.uuid4())!r}')
if MODE == "model_mismatch":
    source = source.replace('"model": "fixture-model"', '"model": "other-model"')
if MODE == "cumulative":
    source = source.replace('"num_turns": 1', '"num_turns": 3').replace('"total_tokens": 10', '"total_tokens": 30')
exec(compile(source, FIXTURE_PATH, "exec"), {"__name__": "__main__", "__file__": FIXTURE_PATH})

"""无 Asterun 依赖的本地 Claude JSON 协议进程；不访问账户或网络。"""
import json
import sys
from pathlib import Path

args = sys.argv
assert args[args.index("--tools") + 1] == ""
assert args[args.index("--mcp-config") + 1] == '{"mcpServers":{}}'
assert "--strict-mcp-config" in args and "--disable-slash-commands" in args
assert args[args.index("--disallowed-tools") + 1] == "*"
assert args[args.index("--permission-mode") + 1] == "dontAsk"
assert args[args.index("--setting-sources") + 1] == ""
assert json.loads(args[args.index("--settings") + 1])["disableAllHooks"] is True
assert "--no-session-persistence" in args and "--no-chrome" in args
context = json.loads(args[args.index("-p") + 1].split("\n", 1)[1])
assert str(Path.cwd()) != context["target"]["root"]
assert not Path("note.txt").exists()
findings = []
if "quality-fixture" in context["objective"] and context["files"][0].get("content") != "answer = 42\n":
    findings = [{"summary": "answer 应为 42", "path": "note.txt", "line": 1}]
print(json.dumps({"result": json.dumps({"target_hash": context["target"]["hash"], "findings": findings}),
                  "session_id": "isolated-review"}))

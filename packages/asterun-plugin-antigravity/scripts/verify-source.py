"""核验固定来源摘要与可复现机械变换；使用当前源码，无需历史 Git 对象。"""
from pathlib import Path
import ast
import hashlib
import json
import sys

package = Path(__file__).resolve().parents[1]
repo = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else package.parents[1]
base = package / "src/asterun_plugin_antigravity"
proof_bytes = (base / "vendor-source.json").read_bytes()
assert hashlib.sha256(proof_bytes).hexdigest() == "01e258ce8451cbe78b1a1a39367f4e32a666055e9477980ce367ed71b08eae06", "来源清单摘要不匹配"
proof = json.loads(proof_bytes)
assert proof["source_commit"] == "f66fae9557ec7bdfe2b3e653b014a8908a0b1e26"
for row in proof["files"]:
    raw = (repo / row["source"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == row["source_sha256"], row["source"]
    text = raw.decode("utf-8")
    if row["extract"]:
        node = next(item for item in ast.parse(text).body
                    if isinstance(item, ast.FunctionDef) and item.name == row["extract"])
        text = row["prefix"] + ast.get_source_segment(text, node) + "\n"
    for old, new in row["replacements"]:
        assert old in text
        text = text.replace(old, new)
    transformed = text.encode("utf-8")
    assert (base / row["target"]).read_bytes() == transformed, row["target"]
    assert hashlib.sha256(transformed).hexdigest() == row["vendored_sha256"], row["target"]
print(json.dumps({"source_commit": proof["source_commit"], "verified_files": len(proof["files"]),
                  "original_sources_unchanged": True, "source_git_object_checked": False,
                  "verification_basis": "pinned-file-sha256-and-mechanical-transform",
                  "manifest_sha256": hashlib.sha256(proof_bytes).hexdigest()}))

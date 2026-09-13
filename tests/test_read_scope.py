from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from asterun.errors import AsterunError
from asterun.read_scope import FixedReadScope, MAX_OUTPUT


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_scope(tmp_path, files=None):
    root = tmp_path / "workspace"
    root.mkdir()
    entries = []
    for name, data in (files or {"source/a.txt": b"one\ntwo\nthree\n"}).items():
        path = root / "snapshot" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entries.append({"name": name, "path": path.relative_to(root).as_posix(),
                        "sha256": sha(data), "size": len(data)})
    manifest = root / "read.json"
    manifest.write_text(json.dumps({"schema": "asterun-read-scope/v1", "files": entries}),
                        encoding="utf-8")
    binding = {"manifest_path": "read.json", "manifest_sha256": sha(manifest.read_bytes())}
    return root, manifest, binding, FixedReadScope(root, binding)


def rebound(root, manifest, document):
    manifest.write_text(json.dumps(document), encoding="utf-8")
    return FixedReadScope(root, {"manifest_path": "read.json",
                                 "manifest_sha256": sha(manifest.read_bytes())})


def bounded(result):
    assert len(json.dumps(result).encode("utf-8")) <= MAX_OUTPUT
    return result


def test_spec_binding_and_manifest_copy(tmp_path):
    root, manifest, binding, scope = make_scope(tmp_path)
    assert scope.binding == binding
    assert scope.tool_spec()["name"] == "asterun_read"
    assert scope.tool_spec()["type"] == "function"
    assert scope.tool_spec()["deferLoading"] is False
    copy = scope.files
    copy[0]["path"] = "outside"
    assert scope.files[0]["path"] != "outside"
    assert scope.call({"op": "list"})["files"][0]["name"] == "source/a.txt"
    assert "path" not in scope.call({"op": "list"})["files"][0]


def test_read_lines_and_eof(tmp_path):
    *_, scope = make_scope(tmp_path)
    first = scope.call({"op": "read", "name": "source/a.txt", "max_lines": 2})
    assert [row["text"] for row in first["lines"]] == ["one", "two"]
    assert first["next_start_line"] == 3
    last = scope.call({"op": "read", "name": "source/a.txt", "start_line": 3})
    assert last["lines"][0]["text"] == "three"
    assert last["next_start_line"] is None and last["truncated"] is False
    assert scope.call({"op": "read", "name": "source/a.txt", "start_line": 4})["lines"] == []


def test_empty_text(tmp_path):
    *_, scope = make_scope(tmp_path, {"empty.txt": b""})
    result = scope.call({"op": "read", "name": "empty.txt"})
    assert result["total_lines"] == 0 and result["lines"] == []
    assert scope.call({"op": "search", "query": "x"})["matches"] == []


def test_list_stable_paging_and_prefix(tmp_path):
    files = {f"source/{index:03}.txt": b"x" for index in reversed(range(205))}
    *_, scope = make_scope(tmp_path, files)
    first = bounded(scope.call({"op": "list", "limit": 200}))
    second = bounded(scope.call({"op": "list", "offset": first["next_offset"]}))
    assert [r["name"] for r in first["files"] + second["files"]] == sorted(files)
    assert second["next_offset"] is None
    assert scope.call({"op": "list", "prefix": "source/20"})["total"] == 5


def test_search_paging_no_dropped_matching_lines(tmp_path):
    files = {f"source/{i}.txt": b"hit\nno\nhit hit\n" * 13 for i in range(3)}
    *_, scope = make_scope(tmp_path, files)
    rows, cursor = [], 0
    while True:
        page = bounded(scope.call({"op": "search", "query": "hit", "cursor": cursor, "limit": 7}))
        rows.extend(page["matches"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    expected = [(name, n) for name in sorted(files) for n, line in
                enumerate(files[name].decode().splitlines(), 1) if "hit" in line]
    assert [(r["name"], r["line"]) for r in rows] == expected


def test_search_literal_not_regex(tmp_path):
    *_, scope = make_scope(tmp_path, {"a": b".*\nanything\n"})
    result = scope.call({"op": "search", "query": ".*"})
    assert len(result["matches"]) == 1
    assert result["matches"][0]["line"] == 1


@pytest.mark.parametrize("name", ["../secret", "/etc/passwd", "x/../secret", "x//a", "x\\a", "./a"])
def test_read_path_escape_rejected(tmp_path, name):
    *_, scope = make_scope(tmp_path)
    with pytest.raises(AsterunError):
        scope.call({"op": "read", "name": name})


def test_unlisted_workspace_file_rejected(tmp_path):
    root, _, _, scope = make_scope(tmp_path)
    (root / "secret").write_text("private")
    with pytest.raises(AsterunError):
        scope.call({"op": "read", "name": "secret"})


@pytest.mark.parametrize("path", ["../secret", "/etc/passwd", "snapshot/../secret", "snapshot//a"])
def test_manifest_unsafe_path_rejected(tmp_path, path):
    root, manifest, _, _ = make_scope(tmp_path)
    doc = json.loads(manifest.read_text())
    doc["files"][0]["path"] = path
    with pytest.raises(AsterunError):
        rebound(root, manifest, doc)


@pytest.mark.parametrize("which", ["manifest", "manifest_parent", "file", "file_parent", "root_parent"])
def test_no_symlinks_in_any_parent_or_final_component(tmp_path, which):
    root, manifest, binding, scope = make_scope(tmp_path)
    if which == "manifest":
        moved = root / "manifest-real.json"
        manifest.rename(moved)
        manifest.symlink_to(moved)
        action = lambda: scope.call({"op": "list"})
    elif which == "manifest_parent":
        (root / "real").mkdir()
        manifest.rename(root / "real" / "read.json")
        (root / "linked").symlink_to(root / "real", target_is_directory=True)
        binding["manifest_path"] = "linked/read.json"
        action = lambda: FixedReadScope(root, binding)
    elif which == "root_parent":
        (tmp_path / "linked").symlink_to(root, target_is_directory=True)
        action = lambda: FixedReadScope(tmp_path / "linked", binding)
    else:
        target = root / "snapshot" / "source" / "a.txt" if which == "file" else root / "snapshot"
        moved = target.with_name(target.name + "-real")
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=moved.is_dir())
        action = lambda: scope.call({"op": "read", "name": "source/a.txt"})
    with pytest.raises(AsterunError):
        action()


def test_fifo_is_rejected_without_blocking(tmp_path):
    root, _, _, scope = make_scope(tmp_path)
    path = root / "snapshot/source/a.txt"
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(AsterunError):
        scope.call({"op": "read", "name": "source/a.txt"})


def test_manifest_changed_after_construction_rejected(tmp_path):
    _, manifest, _, scope = make_scope(tmp_path)
    manifest.write_bytes(manifest.read_bytes() + b" ")
    with pytest.raises(AsterunError, match="摘要"):
        scope.call({"op": "list"})


@pytest.mark.parametrize("content", [b"ONE\ntwo\nthree\n", b"short"])
def test_file_hash_and_size_changed_before_read_rejected(tmp_path, content):
    root, _, _, scope = make_scope(tmp_path)
    (root / "snapshot/source/a.txt").write_bytes(content)
    with pytest.raises(AsterunError):
        scope.call({"op": "read", "name": "source/a.txt"})


def test_mutation_while_reading_rejected(tmp_path, monkeypatch):
    root, _, _, scope = make_scope(tmp_path)
    path = root / "snapshot/source/a.txt"
    original = os.read
    def changing_read(fd, count):
        data = original(fd, count)
        if data == b"one\ntwo\nthree\n":
            path.write_bytes(b"ONE\ntwo\nthree\n")
        return data
    monkeypatch.setattr(os, "read", changing_read)
    with pytest.raises(AsterunError, match="变化"):
        scope.call({"op": "read", "name": "source/a.txt"})


@pytest.mark.parametrize("data", [b"\xff\xfe", b"abc\x00def", b"abc\x01def"])
def test_binary_or_invalid_utf8_rejected_in_read_and_search(tmp_path, data):
    *_, scope = make_scope(tmp_path, {"a": b"hit\n", "z": data})
    with pytest.raises(AsterunError):
        scope.call({"op": "read", "name": "z"})
    # 即使首页已有足量匹配，也不能绕过后面的无效文件。
    with pytest.raises(AsterunError):
        scope.call({"op": "search", "query": "hit", "limit": 1})


def test_long_unicode_line_continuation_is_lossless_and_bounded(tmp_path):
    text = '汉字"\\' * 12000
    *_, scope = make_scope(tmp_path, {"a": (text + "\nend\n").encode()})
    line, column, pieces = 1, 0, []
    for _ in range(30):
        page = bounded(scope.call({"op": "read", "name": "a", "start_line": line,
                                   "start_column": column, "max_lines": 1}))
        pieces.extend(row["text"] for row in page["lines"])
        if page["next_start_line"] != 1:
            assert page["next_start_line"] == 2
            break
        assert page["next_start_column"] > column
        line, column = page["next_start_line"], page["next_start_column"]
    else:
        pytest.fail("长行续读未前进")
    assert "".join(pieces) == text


def test_long_search_excerpt_points_to_readable_match(tmp_path):
    text = "a" * 70000 + "needle" + "汉" * 60000
    *_, scope = make_scope(tmp_path, {"a": text.encode()})
    result = bounded(scope.call({"op": "search", "query": "needle"}))
    row = result["matches"][0]
    assert row["column"] == 70000 and row["truncated"] is True
    assert "needle" in row["text"]
    read = bounded(scope.call({"op": "read", "name": "a", "start_column": row["column"]}))
    assert read["lines"][0]["text"].startswith("needle")


def test_output_budget_does_not_drop_blank_lines(tmp_path):
    *_, scope = make_scope(tmp_path, {"a": b"a" * 60000 + b"\n" * 101})
    seen, line, column = [], 1, 0
    while True:
        page = bounded(scope.call({"op": "read", "name": "a", "start_line": line,
                                   "start_column": column, "max_lines": 400}))
        seen.extend(page["lines"])
        if page["next_start_line"] is None:
            break
        assert (page["next_start_line"], page["next_start_column"]) > (line, column)
        line, column = page["next_start_line"], page["next_start_column"]
    assert [row["line"] for row in seen] == list(range(1, 102))


def test_long_virtual_names_paginate_within_json_budget(tmp_path):
    root, manifest, _, _ = make_scope(tmp_path, {str(i): b"hit\n" for i in range(30)})
    doc = json.loads(manifest.read_text())
    for entry in doc["files"]:
        entry["name"] = ("汉" * 100 + "/") * 10 + entry["name"]
    scope = rebound(root, manifest, doc)
    expected = sorted(entry["name"] for entry in doc["files"])
    seen, cursor = [], 0
    while True:
        page = bounded(scope.call({"op": "list", "offset": cursor}))
        seen.extend(row["name"] for row in page["files"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > cursor
        cursor = page["next_offset"]
    assert seen == expected
    seen, cursor = [], 0
    while True:
        page = bounded(scope.call({"op": "search", "query": "hit", "cursor": cursor}))
        seen.extend(row["name"] for row in page["matches"])
        if page["next_cursor"] is None:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert seen == expected


@pytest.mark.parametrize("args", [
    {"op": "shell"}, {"op": "read", "name": "source/a.txt", "command": "cat"},
    {"op": "search", "query": ""}, {"op": "search", "query": "a\nb"},
    {"op": "search", "query": "x", "limit": 101}, {"op": "search", "query": "x", "cursor": -1},
    {"op": "list", "limit": 201}, {"op": "list", "limit": True},
    {"op": "read", "name": "source/a.txt", "max_lines": 401},
    {"op": "read", "name": "source/a.txt", "start_column": 99},
    {"op": "search", "query": "x" * 20000}, {"op": "list", "prefix": []},
    {"op": "list", "prefix": "../"}, {"op": "search", "query": "x", "prefix": "/"},
    {"op": ["list"]}, {"op": "search", "query": "\ud800"},
])
def test_malformed_or_excessive_calls_fail_as_asterun_error(tmp_path, args):
    *_, scope = make_scope(tmp_path)
    with pytest.raises(AsterunError):
        scope.call(args)


@pytest.mark.parametrize("mutation", ["duplicate_name", "duplicate_path", "oversize_file", "oversize_total", "too_many", "bad_hash"])
def test_manifest_bounds_and_duplicates(tmp_path, mutation):
    root, manifest, _, _ = make_scope(tmp_path)
    doc = json.loads(manifest.read_text())
    entry = doc["files"][0]
    if mutation == "duplicate_name":
        doc["files"].append({**entry, "path": "elsewhere"})
    elif mutation == "duplicate_path":
        doc["files"].append({**entry, "name": "elsewhere"})
    elif mutation == "oversize_file":
        entry["size"] = 64 * 1024 * 1024 + 1
    elif mutation == "oversize_total":
        entry["size"] = 64 * 1024 * 1024
        doc["files"].append({**entry, "name": "other", "path": "other"})
    elif mutation == "too_many":
        doc["files"] *= 20002
    else:
        entry["sha256"] = "not-a-hash"
    with pytest.raises(AsterunError):
        rebound(root, manifest, doc)


def test_manifest_size_limit_and_duplicate_json_keys(tmp_path):
    root, manifest, _, _ = make_scope(tmp_path)
    for raw in [b" " * (8 * 1024 * 1024 + 1),
                b'{"schema":"asterun-read-scope/v1","files":[],"files":[]}']:
        manifest.write_bytes(raw)
        with pytest.raises(AsterunError):
            FixedReadScope(root, {"manifest_path": "read.json", "manifest_sha256": sha(raw)})

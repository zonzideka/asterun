from __future__ import annotations

from pathlib import Path

import pytest

from asterun.errors import PATH_OUT_OF_SCOPE, AsterunError
from asterun.paths import resolve_within


def test_relative_path_inside_root(workspace_root: Path) -> None:
    resolved = resolve_within(workspace_root, "note.txt")
    assert resolved == (workspace_root / "note.txt").resolve()
    assert resolved.is_file()


def test_absolute_path_inside_root(workspace_root: Path) -> None:
    target = workspace_root / "note.txt"
    assert resolve_within(workspace_root, target) == target.resolve()


def test_absolute_path_outside_root(workspace_root: Path, isolated_env: Path) -> None:
    outside = isolated_env / "secret.txt"
    outside.write_text("no", encoding="utf-8")
    with pytest.raises(AsterunError) as captured:
        resolve_within(workspace_root, outside)
    assert captured.value.code == PATH_OUT_OF_SCOPE


def test_parent_directory_escape(workspace_root: Path, isolated_env: Path) -> None:
    outside = isolated_env / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    with pytest.raises(AsterunError) as captured:
        resolve_within(workspace_root, "../outside.txt")
    assert captured.value.code == PATH_OUT_OF_SCOPE


def test_symlink_escape_is_rejected(workspace_root: Path, isolated_env: Path) -> None:
    outside = isolated_env / "leaked.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace_root / "link"
    link.symlink_to(outside)
    with pytest.raises(AsterunError) as captured:
        resolve_within(workspace_root, "link")
    assert captured.value.code == PATH_OUT_OF_SCOPE
    assert "leaked.txt" in captured.value.details["resolved"]


def test_symlink_inside_root_is_allowed(workspace_root: Path) -> None:
    target = workspace_root / "note.txt"
    link = workspace_root / "alias"
    link.symlink_to(target)
    assert resolve_within(workspace_root, "alias") == target.resolve()


def test_root_boundary_dot_allowed_parent_denied(workspace_root: Path) -> None:
    assert resolve_within(workspace_root, ".") == workspace_root.resolve()
    with pytest.raises(AsterunError) as captured:
        resolve_within(workspace_root, "..")
    assert captured.value.code == PATH_OUT_OF_SCOPE

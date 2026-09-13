from __future__ import annotations

from pathlib import Path

from asterun.errors import PATH_INVALID, PATH_OUT_OF_SCOPE, AsterunError


def as_absolute(path: Path) -> Path:
    if not path.is_absolute():
        raise AsterunError(
            PATH_INVALID,
            f"路径必须是绝对路径：{path}",
            next_action="在配置或请求中提供绝对路径，或使用相对于工作区根的相对路径",
            details={"path": str(path)},
        )
    return path


def resolve_allowed_root(root: Path) -> Path:
    if not root.is_absolute():
        raise AsterunError(
            PATH_INVALID,
            f"工作区根必须是绝对路径：{root}",
            next_action="把 workspace.root 写成绝对路径，或写成相对于配置文件的路径",
            details={"path": str(root)},
        )
    resolved = root.resolve()
    if not resolved.exists():
        raise AsterunError(
            PATH_INVALID,
            f"工作区根不存在：{resolved}",
            next_action="创建该目录，或把配置指向当前环境中的真实临时/工作目录",
            details={"path": str(resolved)},
        )
    if not resolved.is_dir():
        raise AsterunError(
            PATH_INVALID,
            f"工作区根不是目录：{resolved}",
            next_action="将 workspace.root 指向一个目录",
            details={"path": str(resolved)},
        )
    return resolved


def resolve_within(root: Path, user_path: str | Path) -> Path:
    """在真实文件系统上解析路径，跟随符号链接，并强制落在允许根内。

    不只做字符串前缀匹配：会 resolve() 后再用 relative_to 检查边界。
    """
    root_real = resolve_allowed_root(root)
    raw = Path(user_path)
    candidate = raw if raw.is_absolute() else (root_real / raw)
    real = candidate.resolve()
    try:
        real.relative_to(root_real)
    except ValueError as exc:
        raise AsterunError(
            PATH_OUT_OF_SCOPE,
            f"路径越出允许根：{real} 不在 {root_real} 内",
            next_action="使用工作区根内的相对路径，不要使用父目录跳转或指向根外的符号链接",
            details={"path": str(user_path), "resolved": str(real), "allowed_root": str(root_real)},
        ) from exc
    return real

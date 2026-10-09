"""构建并安装三个插件 wheel，导入 worker 并做一次钉定注册。"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = (
    "asterun-plugin-antigravity",
    "asterun-plugin-cursor",
    "asterun-plugin-jules",
)


def run(args: list[str], *, env: dict[str, str], cwd: Path) -> None:
    result = subprocess.run(args, env=env, cwd=cwd, text=True)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def main() -> None:
    tmp = Path(os.environ.get("ASTERUN_PLUGIN_SMOKE_TMP") or tempfile.mkdtemp(prefix="asterun-plugin-smoke."))
    tmp.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).resolve().parent), "/usr/bin", "/bin"]),
        "HOME": str(tmp / "home"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    }
    Path(env["HOME"]).mkdir(mode=0o700, exist_ok=True)
    venv = tmp / "venv"
    run([sys.executable, "-m", "venv", str(venv)], env=env, cwd=tmp)
    python = venv / "bin" / "python"
    run([str(python), "-m", "pip", "install", "-q", "-U", "pip", "setuptools", "wheel"], env=env, cwd=tmp)
    run([str(python), "-m", "pip", "install", "-q", "-e", str(ROOT)], env=env, cwd=tmp)
    wheels = tmp / "wheels"
    wheels.mkdir()
    for name in PACKAGES:
        before = set(wheels.glob("*.whl"))
        run([str(python), "-m", "pip", "wheel", "--no-build-isolation", "--no-deps",
             "-w", str(wheels), str(ROOT / "packages" / name)], env=env, cwd=tmp)
        created = set(wheels.glob("*.whl")) - before
        if len(created) != 1:
            raise SystemExit(f"{name} 没有产出唯一 wheel：{sorted(created)}")
        run([str(python), "-m", "pip", "install", "-q", "--no-deps", str(created.pop())], env=env, cwd=tmp)
    checker = r'''
import hashlib, importlib, json, sys, sysconfig
from pathlib import Path
from asterun.plugins.registry import PluginRegistry, installation_digest
items = json.loads(sys.argv[1])
library = Path(sysconfig.get_path("purelib")).resolve()
for item in items:
    module = importlib.import_module(item["module"])
    location = Path(module.__file__).resolve()
    if library not in location.parents:
        raise SystemExit(f"导入路径不在安装目录中：{location}")
    runtime = location.parent
    wheel = Path(item["wheel"])
    registry = PluginRegistry()
    registration = registry.register_external(
        runtime / "manifest.json",
        runner=[sys.executable, "-m", item["module"]],
        installation_path=wheel,
        installation_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        enabled=False,
        runtime_path=runtime,
        runtime_sha256=installation_digest(runtime),
    )
    checked = registry.verify_installation(registration.plugin_id)
    if checked.runtime_path is None:
        raise SystemExit("注册后仍未钉定运行代码")
print("plugin wheel smoke passed")
'''
    specs = []
    for name, module in (
        ("asterun_plugin_antigravity", "asterun_plugin_antigravity.worker"),
        ("asterun_plugin_cursor", "asterun_plugin_cursor.worker"),
        ("asterun_plugin_jules", "asterun_plugin_jules.worker"),
    ):
        matches = sorted(wheels.glob(f"{name}-*.whl"))
        if len(matches) != 1:
            raise SystemExit(f"未找到唯一的 {name} wheel：{matches}")
        specs.append({"module": module, "wheel": str(matches[0])})
    run([str(python), "-c", checker, json_dumps(specs)], env=env, cwd=tmp)
    print(f"plugin wheel smoke passed in {tmp}")


def json_dumps(value) -> str:
    import json
    return json.dumps(value)


if __name__ == "__main__":
    main()

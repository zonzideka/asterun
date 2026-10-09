"""构建并安装三个插件 wheel，钉定注册后经隔离启动调用 plugin.describe。"""
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
from asterun.plugins.registry import PluginRegistry, installation_digest, isolated_worker_argv
from asterun.plugins.worker import WorkerHost
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
        enabled=True,
        runtime_path=runtime,
        runtime_sha256=installation_digest(runtime),
    )
    checked = registry.verify_installation(registration.plugin_id)
    if checked.runtime_path is None:
        raise SystemExit("注册后仍未钉定运行代码")
    argv = isolated_worker_argv(checked.runner, checked.runtime_path)
    if argv[1:4] != ["-I", "-S", "-B"] or argv[-1] != item["module"]:
        raise SystemExit(f"隔离启动参数不符合预期：{argv[1:4]} {argv[-1]}")
    reply = WorkerHost(registry, checked).call("plugin.describe", {}, {}, cwd=runtime)
    manifest = reply.result.get("manifest")
    if (reply.result.get("protocol_version") != "asterun-worker/v1"
            or not isinstance(manifest, dict) or manifest.get("plugin_id") != item["plugin_id"]):
        raise SystemExit(f"隔离启动没有返回该插件的描述：{item['module']}")
print("plugin wheel smoke passed")
'''
    specs = []
    for name, module, plugin_id in (
        ("asterun_plugin_antigravity", "asterun_plugin_antigravity.worker", "google.antigravity-cli"),
        ("asterun_plugin_cursor", "asterun_plugin_cursor.worker", "cursor.agent"),
        ("asterun_plugin_jules", "asterun_plugin_jules.worker", "google.jules"),
    ):
        matches = sorted(wheels.glob(f"{name}-*.whl"))
        if len(matches) != 1:
            raise SystemExit(f"未找到唯一的 {name} wheel：{matches}")
        specs.append({"module": module, "wheel": str(matches[0]), "plugin_id": plugin_id})
    run([str(python), "-c", checker, json_dumps(specs)], env=env, cwd=tmp)
    print(f"plugin wheel smoke passed in {tmp}")


def json_dumps(value) -> str:
    import json
    return json.dumps(value)


if __name__ == "__main__":
    main()

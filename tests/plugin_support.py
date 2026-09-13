"""只用本地受审 fixture wheel 创建独立插件环境。"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import sys


def build_external_fake(root: Path, *, tools_only=False):
    source = root / "source"
    shutil.copytree(Path(__file__).parent / "fixtures/plugins/external_fake", source)
    if tools_only:
        manifest_path = source / "src/asterun_external_fake/manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["roles"] = ["capability"]
        manifest["capabilities"] = [row for row in manifest["capabilities"] if row["name"] != "agent.execute"]
        manifest_path.write_text(json.dumps(manifest))
    env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/bin", "/bin"]),
           "HOME": str(root / "home"), "PIP_NO_INDEX": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    Path(env["HOME"]).mkdir()
    def run(args):
        result = subprocess.run(args, env=env, cwd=root, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr
        return result.stdout
    run([sys.executable, "-m", "pip", "wheel", "--no-build-isolation", "--no-deps", "-w", str(root / "wheels"), str(source)])
    wheel = next((root / "wheels").glob("*.whl"))
    run([sys.executable, "-m", "venv", str(root / "worker-env")])
    python = root / "worker-env/bin/python"
    run([str(python), "-m", "pip", "install", "--no-deps", str(wheel)])
    library = Path(run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"]).strip())
    return {"wheel": wheel, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "manifest": library / "asterun_external_fake/manifest.json",
            "runner": [str(python), "-m", "asterun_external_fake.worker"], "env": env,
            "runtime_path": library}

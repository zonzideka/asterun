from pathlib import Path
from .compat import AsterunError, INVALID_CONFIG

def build_command(binary: Path, model: str) -> list[str]:
    if not model or model != model.strip() or "\x00" in model or model.startswith("-"):
        raise AsterunError(INVALID_CONFIG, "Antigravity 必须配置明确、有效的 model ID")
    return [str(binary), "--input-format", "stream-json", "--output-format", "stream-json",
            "--model", model, "--disable-slash-commands", "--print-timeout", "5m"]

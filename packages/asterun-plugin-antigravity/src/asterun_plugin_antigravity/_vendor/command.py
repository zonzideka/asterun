from pathlib import Path
from .compat import AsterunError, INVALID_CONFIG

def build_command(binary: Path, model: str, timeout_seconds: int = 300) -> list[str]:
    if not model or model != model.strip() or "\x00" in model or model.startswith("-"):
        raise AsterunError(INVALID_CONFIG, "Antigravity 必须配置明确、有效的 model ID")
    # 30..3600 与配置校验共用。插件按函数摘取本段，不能引用模块级名字。
    if type(timeout_seconds) is not int or not 30 <= timeout_seconds <= 3600:
        raise AsterunError(INVALID_CONFIG, "Antigravity timeout_seconds 必须为 30..3600 的整数")
    print_timeout = f"{timeout_seconds // 60}m" if timeout_seconds % 60 == 0 else f"{timeout_seconds}s"
    return [str(binary), "--input-format", "stream-json", "--output-format", "stream-json",
            "--model", model, "--disable-slash-commands", "--print-timeout", print_timeout]

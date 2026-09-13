"""独立第三方测试插件。核心发现阶段绝不能执行此模块。"""
import os
from pathlib import Path

if os.environ.get("EXTERNAL_FAKE_IMPORT_TRAP"):
    Path(os.environ["EXTERNAL_FAKE_IMPORT_TRAP"]).write_text("plugin imported")

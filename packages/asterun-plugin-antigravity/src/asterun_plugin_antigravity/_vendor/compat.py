"""仅供冻结运行器使用的兼容值；不导入核心或供应商 SDK。"""
from dataclasses import dataclass

INVALID_CONFIG = "INVALID_CONFIG"
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"
REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"


class AsterunError(Exception):
    def __init__(self, code, message, *, next_action=""):
        super().__init__(message)
        self.code, self.message, self.next_action = code, message, next_action

    def to_dict(self):
        return {"code": self.code, "message": self.message, "next_action": self.next_action}


@dataclass(frozen=True, slots=True)
class RunId:
    value: str

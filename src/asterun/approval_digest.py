"""保持既有审批目标摘要的跨后端公共实现。"""
import hashlib
import json


def request_hash(input_hash, run_id, request):
    raw = json.dumps([input_hash, run_id, request], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()

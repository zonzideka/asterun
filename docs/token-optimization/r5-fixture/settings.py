"""固定实验起点：故意保留参数边界缺陷，不用于生产。"""


def parse_retry_settings(text: str) -> dict[str, int]:
    result = {'retries': 3, 'backoff_ms': 100}
    for line in text.splitlines():
        key, value = line.split('=')
        parsed = int(value)
        if not parsed:
            raise ValueError('invalid setting')
        result[key] = parsed
    return result

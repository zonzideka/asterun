#!/usr/bin/env python3
"""核对三种调用路径的配对样本；只处理离线报告，绝不调用模型。"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import statistics

ARMS = ('direct', 'asterun_baseline', 'asterun_candidate')
CONTEXT = {'snapshot_sha256', 'model', 'cli_version', 'tool_permissions', 'context_policy', 'concurrency', 'quality_gate'}
TOKENS = ('controller_tokens', 'executor_tokens', 'reviewer_tokens')
METRICS = (*TOKENS, 'repairs', 'read_bytes', 'wait_seconds', 'manual_interventions', 'elapsed_seconds')
KEYS = {'case', 'trial', 'arm', 'context', 'implementation_revision', 'quality_passed', 'completed', 'simulated', *METRICS}


def compare(rows):
    if not isinstance(rows, list) or not 18 <= len(rows) <= 90:
        raise ValueError('需要 6—10 个任务、每组 1—3 次、三条路径完整配对，最多 90 行')
    groups = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != KEYS:
            raise ValueError('样本字段不完整或有未知字段；未测量指标显式填 null')
        if (not isinstance(row['case'], str) or not 1 <= len(row['case']) <= 100
                or type(row['trial']) is not int or not 1 <= row['trial'] <= 3 or row['arm'] not in ARMS
                or not isinstance(row['implementation_revision'], str) or not row['implementation_revision']):
            raise ValueError('任务、轮次、路径或实现版本无效')
        if (not isinstance(row['context'], dict) or set(row['context']) != CONTEXT
                or any(not isinstance(v, str) or not 1 <= len(v) <= 512 for v in row['context'].values())):
            raise ValueError('必须固定输入快照、模型、CLI、工具权限、上下文策略、并发和质量门')
        if any(type(row[key]) is not bool for key in ('quality_passed', 'completed', 'simulated')):
            raise ValueError('完成、质量和模拟标记需要布尔值')
        for name in METRICS:
            value = row[name]
            integral = name not in {'wait_seconds', 'elapsed_seconds'}
            if value is not None and (type(value) not in {int, float} or not math.isfinite(value) or value < 0
                                      or integral and type(value) is not int):
                raise ValueError('用量与耗时必须是非负实测值或 null，计数使用整数')
        group = groups.setdefault((row['case'], row['trial']), {})
        if row['arm'] in group:
            raise ValueError('同一任务、轮次和路径重复')
        group[row['arm']] = row
    if not 6 <= len({key[0] for key in groups}) <= 10:
        raise ValueError('首轮任务集限定 6—10 项')
    eligible = True
    for group in groups.values():
        if set(group) != set(ARMS):
            raise ValueError('缺少配对路径，不将不同任务或轮次拼成对照')
        base = group['direct']
        if any(row['context'] != base['context'] for row in group.values()):
            raise ValueError('配对条件不一致，不能计算节省比例')
        eligible = eligible and all(not row['simulated'] and row['completed'] and row['quality_passed']
                                    and all(row[key] is not None for key in TOKENS) for row in group.values())
    summary = {}
    for arm in ARMS:
        samples = [group[arm] for group in groups.values()]
        summary[arm] = {'samples': len(samples), 'quality_passed': sum(r['quality_passed'] for r in samples),
                        'completed': sum(r['completed'] for r in samples),
                        'medians': {name: statistics.median(r[name] for r in samples)
                                    if all(r[name] is not None for r in samples) else None for name in METRICS},
                        'total_tokens': sum(r[key] for r in samples for key in TOKENS)
                                    if all(r[key] is not None for r in samples for key in TOKENS) else None}
    savings = {arm: None for arm in ARMS[1:]}
    baseline = summary['direct']['total_tokens']
    if eligible and baseline:
        savings = {arm: 100 * (baseline - summary[arm]['total_tokens']) / baseline for arm in ARMS[1:]}
    return {'source': 'external_reported', 'independently_verified': False, 'paired_samples': len(groups),
            'savings_comparable': eligible, 'summary': summary, 'token_savings_percent_vs_direct': savings,
            'note': '模拟、缺失用量或质量未通过时不计算节省比例；订阅实付费用不由 Token 推算。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.input.stat().st_size > 1024 * 1024:
        parser.error('样本文件超过 1 MiB')
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('重复 JSON 字段')
                result[key] = value
            return result
        result = compare(json.loads(args.input.read_text(), object_pairs_hook=unique))
    except (ValueError, TypeError, OverflowError) as error:
        parser.error(str(error))
    # 不覆盖已有实验产物。
    with args.output.open('x') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()

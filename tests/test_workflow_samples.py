from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('workflow_samples', Path(__file__).resolve().parents[1] / 'scripts/compare-workflow-samples.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def samples():
    return [{'case': str(case), 'trial': 1, 'arm': arm, 'context': {key: 'fixed' for key in module.CONTEXT},
             'implementation_revision': arm, 'quality_passed': True, 'completed': True, 'simulated': True,
             **{key: None for key in module.METRICS}} for case in range(6) for arm in module.ARMS]


def test_fake_protocol_reports_never_claim_savings():
    result = module.compare(samples())
    assert result['paired_samples'] == 6
    assert all(value is None for value in result['token_savings_percent_vs_direct'].values())
    assert not result['savings_comparable']


@pytest.mark.parametrize('broken', ['context', 'duplicate', 'incomplete', 'counter', 'boolean'])
def test_invalid_pairing_fails_closed(broken):
    rows = samples()
    if broken == 'context': rows[0]['context']['model'] = 'different'
    if broken == 'duplicate': rows[-1] = deepcopy(rows[0])
    if broken == 'incomplete': rows.pop()
    if broken == 'counter': rows[0]['executor_tokens'] = -1
    if broken == 'boolean': rows[0]['controller_tokens'] = True
    with pytest.raises(ValueError): module.compare(rows)


def test_complete_report_math_includes_controller_executor_and_review_costs():
    # 此处纯数值夹具，不代表真实模型测量。
    rows = samples()
    for row in rows:
        row.update(simulated=False, controller_tokens=20, executor_tokens=70, reviewer_tokens=10)
        if row['arm'] == 'asterun_candidate': row['controller_tokens'] = 10
    result = module.compare(rows)
    assert result['token_savings_percent_vs_direct']['asterun_candidate'] == 10
    rows[0]['quality_passed'] = False
    assert module.compare(rows)['token_savings_percent_vs_direct']['asterun_candidate'] is None
    rows[0]['quality_passed'] = True; rows[0]['reviewer_tokens'] = None
    assert module.compare(rows)['token_savings_percent_vs_direct']['asterun_candidate'] is None

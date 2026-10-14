from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app import field_llm, landscape_reports
from app.landscape_evidence import input_summary
from app.local_llm_stream import LocalStreamError
from app.main import app
from test_landscape_reports import fixture, output, report_for, wait_job


def compact_call(payload, schema, instructions, provider, model=None, *, input_context, **kwargs):
    actual = deepcopy(payload)
    actual['papers'] = [deepcopy(payload['papers'][0]), deepcopy(next(p for p in payload['papers'] if p['side']=='after'))]
    for paper in actual['papers']:
        paper['abstract'] = 'Laser heating was evaluated.'
        paper['abstract_sent_chars'] = len(paper['abstract'])
        paper['abstract_truncated'] = True
    actual['input_summary'] = input_summary(actual['papers'])
    input_context.update(payload=actual, metadata={
        'context_window': 4096, 'input_tokens_estimate': 2100, 'output_tokens': 1365,
        'safety_tokens': 512, 'reduced': True, 'status': 'completed', 'request_attempts': 1,
        'warnings': ['入力上限に合わせて根拠を抜粋しました。'], 'omitted_paper_ids':['paper-1']})
    return output('Strength reached 900 MPa.', ['paper-0','paper-1','paper-8']), 'local_llm', 'small-model'


def test_actual_request_drives_validation_without_changing_original_evidence(fixture, monkeypatch):
    report = report_for(fixture)
    original = deepcopy(report)
    monkeypatch.setattr(field_llm, 'structured_output', compact_call)
    narrative = landscape_reports.generate(report, 'local')
    assert report['movement'] == original['movement']
    assert report['evidence_papers'] == original['evidence_papers']
    assert report['input_summary'] == original['input_summary']
    assert narrative['input_paper_ids'] == ['paper-0','paper-8']
    assert narrative['sections'][0]['unverified_evidence_ids'] == ['paper-1']
    assert any(w['code']=='numeric_mismatch' for w in narrative['validation']['warnings'])
    assert '900' not in report['llm_input']['papers'][0]['abstract']
    assert report['llm_input']['input_summary']['paper_count']==2
    assert any('抜粋' in text for text in narrative['caveats'])
    report['narrative'] = narrative
    csv = landscape_reports.export_csv(report)
    assert 'llm_input_paper' in csv and 'input_tokens_estimate' in csv


@pytest.mark.parametrize('kind', ['context_budget','context_length'])
def test_context_failure_preserves_input_audit_and_calculations_in_saved_api_report(fixture, monkeypatch, kind):
    def fail(*args, **kwargs):
        compact_call(*args, **kwargs)
        kwargs['input_context']['metadata']['status']='failed'
        raise LocalStreamError('secret/raw upstream text', kind=kind)
    monkeypatch.setattr(field_llm, 'structured_output', fail)
    with TestClient(app) as client:
        job = wait_job(client, fixture[0]['id'], provider='local')
        report = client.get('/api/landscape-reports/'+job['landscape_report_id']).json()
        exported=client.get('/api/landscape-reports/'+job['landscape_report_id']+'/export').text
    assert report['generation_error_kind']==kind
    assert report['generation_status']=='failed'
    assert report['movement']['from_count']==8
    assert report['narrative']['mode']=='deterministic'
    assert report['llm_input']['status']=='failed'
    assert report['llm_input']['papers'][0]['abstract']=='Laser heating was evaluated.'
    assert 'secret/raw' not in str(report)
    assert 'llm_input_paper' in exported

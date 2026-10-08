"""Single-prompt runs preserve failures outside each fresh attempt."""
from pathlib import Path
import json
import time

import pytest

from agent_trace_kit import runner
from agent_trace_kit.desk_store import DeskStore
from test_codex_retry_policy import make_job


@pytest.mark.parametrize('exhausted', ['deadline', 'attempts'])
def test_manual_retry_renews_budget_without_overwriting_evidence(tmp_path, monkeypatch, exhausted):
    store = DeskStore(tmp_path / 'desk')
    store.save_settings({'codex_side_max_attempts': 2 if exhausted == 'deadline' else 1})
    job = make_job(store, tmp_path)
    evidence = store.evidence_dir(job['id']) / 'attempts'
    evidence.mkdir()
    old = evidence / 'a-01-stream.jsonl'
    old.write_text('retained failed stream', encoding='utf-8')
    store.update_side(job['id'], 'A', {
        'status': 'failed', 'attempts': [{'attempt': 1, 'status': 'cut'}],
        'clean_deadline_epoch': time.time() - 1 if exhausted == 'deadline' else time.time() + 600,
        'failure_capture': {'origin': 'posthoc-failed-worktree'},
        'check_results': [{'exit_code': 1}],
    })
    store.update_review(job['id'], {'conclusion': 'B 更好', 'reason': '旧产物判断'})
    sibling = store.get_job(job['id'])['sides']['B']
    run = runner.PairRunner(store)

    def prepare(job, dest, branch):
        Path(dest).mkdir()
        return {'workspace': str(dest), 'head': 'baseline'}

    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    calls = []

    def attempt(*args, **kwargs):
        calls.append(kwargs['attempt'])
        return {'code': 1, 'new_session': None, 'completed': False, 'aborted': False,
                'summary': 'fixture permanent failure', 'failure': 'HTTP 401', 'retryable': False}

    monkeypatch.setattr(run, '_run_attempt', attempt)
    monkeypatch.setattr(runner.time, 'sleep', lambda _: pytest.fail('First manual attempt must launch immediately'))
    run.retry_side(job['id'], 'A', enqueue=False)
    run.run_side(job['id'], 'A')
    assert calls == [2], 'An explicit manual rerun must launch a new CLI attempt'
    assert old.read_text(encoding='utf-8') == 'retained failed stream'
    side = store.get_job(job['id'])['sides']['A']
    assert [row['attempt'] for row in side['attempts']] == [1, 2]
    assert side['clean_deadline_epoch'] > time.time()
    assert not side.get('failure_capture') and not side.get('check_results')
    assert store.get_job(job['id'])['review']['conclusion'] == ''
    receipt = next((store.evidence_dir(job['id']) / 'manual-reruns').glob('a-*.json'))
    archived = json.loads(receipt.read_text(encoding='utf-8'))
    assert archived['previous_review']['reason'] == '旧产物判断'
    assert archived['previous_side']['failure_capture']['origin'] == 'posthoc-failed-worktree'
    assert store.get_job(job['id'])['sides']['B'] == sibling
    if exhausted == 'attempts':
        run.run_side(job['id'], 'A')
        assert calls == [2], 'A restart must not grant another manual rerun budget'


def test_clean_policy_cannot_enable_continuation(tmp_path):
    store = DeskStore(tmp_path / 'desk')
    settings = store.save_settings({'codex_completion_recovery': True})
    assert settings['codex_clean_single_turn']
    assert settings['codex_completion_recovery'] is False


@pytest.mark.parametrize('mode', ['project', 'inherit'])
def test_manual_retry_counts_unrecorded_stream_and_preserves_its_path(tmp_path, monkeypatch, mode):
    store = DeskStore(tmp_path / 'desk')
    store.save_settings({'codex_side_max_attempts': 1})
    job = make_job(store, tmp_path)
    if mode == 'inherit':
        store.update_job(job['id'], {'cli_connection': {'mode': 'inherit'}})
    evidence = store.evidence_dir(job['id']) / 'attempts'
    evidence.mkdir()
    stream = evidence / 'a-07-stream.jsonl'
    stream.write_text('crashed before ledger save', encoding='utf-8')
    run = runner.PairRunner(store)

    def prepare(job, dest, branch):
        Path(dest).mkdir()
        return {'workspace': str(dest), 'head': 'baseline'}

    calls = []

    def attempt(*args, **kwargs):
        calls.append(kwargs['attempt'])
        return {'code': 1, 'new_session': None, 'completed': False, 'aborted': True,
                'summary': 'fixture stop', 'failure': 'HTTP 401', 'retryable': False}

    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    monkeypatch.setattr(run, '_run_attempt', attempt)
    run.retry_side(job['id'], 'A', enqueue=False)
    run.run_side(job['id'], 'A')
    assert calls == [8]
    assert stream.read_text(encoding='utf-8') == 'crashed before ledger save'
    assert [row['attempt'] for row in store.get_job(job['id'])['sides']['A']['attempts']] == [7, 8]


def test_manual_retry_preserves_sibling_upload_during_preparation(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    job = make_job(store, tmp_path)
    store.update_side(job['id'], 'B', {'status': 'done'})
    run = runner.PairRunner(store)

    def prepare(job, dest, branch):
        store.update_job(job['id'], {'uploads': {'A': {}, 'B': {'trace_url': 'new-b-upload'}}})
        Path(dest).mkdir()
        return {'workspace': str(dest), 'head': 'baseline'}

    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    run.retry_side(job['id'], 'A', enqueue=False)
    assert store.get_job(job['id'])['uploads']['B'] == {'trace_url': 'new-b-upload'}


def test_failed_manual_preparation_cannot_reuse_old_capture(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    job = make_job(store, tmp_path)
    store.update_side(job['id'], 'A', {'failure_capture': {'origin': 'posthoc-failed-worktree'},
                                     'session_id': 'old', 'jsonl_local': 'old.jsonl'})
    run = runner.PairRunner(store)

    def prepare(*args):
        raise RuntimeError('fixture unavailable baseline')

    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    with pytest.raises(RuntimeError, match='unavailable baseline'):
        run.retry_side(job['id'], 'A', enqueue=False)
    side = store.get_job(job['id'])['sides']['A']
    assert side['status'] == 'failed'
    assert not side['failure_capture'] and not side['session_id'] and not side['jsonl_local']


def test_manual_retry_keeps_frozen_failure_snapshot_at_original_path(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    job = make_job(store, tmp_path)
    snapshot = store.evidence_dir(job['id']) / 'failure-snapshots/frozen/workspace'
    snapshot.mkdir(parents=True)
    (snapshot / 'partial.txt').write_text('冻结失败产物', encoding='utf-8')
    store.update_side(job['id'], 'A', {'workspace': str(snapshot),
                                     'failure_capture': {'origin': 'posthoc-failed-worktree'}})
    run = runner.PairRunner(store)

    def prepare(job, dest, branch):
        Path(dest).mkdir()
        return {'workspace': str(dest), 'head': 'baseline'}

    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    run.retry_side(job['id'], 'A', enqueue=False)
    assert (snapshot / 'partial.txt').read_text(encoding='utf-8') == '冻结失败产物'
    assert store.get_job(job['id'])['sides']['A']['workspace'] == str(store.workspace_pair_dir(job['id']) / 'a')


def test_completion_retry_refuses_missing_original_worktree_before_mutation(tmp_path):
    store = DeskStore(tmp_path / 'desk')
    store.save_settings({'codex_clean_single_turn': False, 'codex_completion_recovery': True})
    job = make_job(store, tmp_path)
    snapshot = store.evidence_dir(job['id']) / 'failure-snapshots/frozen/workspace'
    snapshot.mkdir(parents=True)
    store.update_side(job['id'], 'A', {'workspace': str(snapshot), 'status': 'failed',
                                     'failure_capture': {'origin': 'posthoc-failed-worktree',
                                                         'source_workspace': str(tmp_path / 'missing')}})
    before = store.get_job(job['id'])
    with pytest.raises(RuntimeError, match='原工作区'):
        runner.PairRunner(store).retry_side(job['id'], 'A', enqueue=False)
    assert store.get_job(job['id']) == before
    assert snapshot.is_dir()


def test_service_recovery_keeps_manual_allowance_and_deadline(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    job = make_job(store, tmp_path)
    store.update_job(job['id'], {'baseline_sha': 'baseline', 'status': 'running'})
    deadline = time.time() - 1
    store.update_side(job['id'], 'A', {
        'status': 'running', 'attempts': [{'attempt': 5, 'status': 'cut'}],
        'attempt_budget_start': 4, 'clean_deadline_epoch': deadline,
    })
    run = runner.PairRunner(store)

    def prepare(job, dest, branch):
        Path(dest).mkdir()
        return {'workspace': str(dest), 'head': 'baseline'}

    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    run.recover()
    side = store.get_job(job['id'])['sides']['A']
    assert side['attempt_budget_start'] == 4
    assert side['clean_deadline_epoch'] == deadline
    monkeypatch.setattr(run, '_run_attempt', lambda *a, **k: pytest.fail('Expired recovery must not launch'))
    run.run_side(job['id'], 'A')
    assert '预算' in store.get_job(job['id'])['sides']['A']['error']


def test_fresh_workspace_preserves_old_work_and_clears_bindings(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    job = make_job(store, tmp_path)
    work = Path(job['sides']['A']['workspace'])
    (work / 'partial.txt').write_text('失败现场', encoding='utf-8')
    store.update_side(job['id'], 'A', {'completion_recovery': True, 'resumed_session_id': 'old',
                                      'video_local': 'old.mp4', 'video_url': 'https://old',
                                      'demo': {'status': 'ready'}})
    run = runner.PairRunner(store)
    def prepare(job, dest, branch):
        assert not Path(dest).exists()
        Path(dest).mkdir()
        return {'workspace': str(dest), 'head': 'baseline'}
    monkeypatch.setattr(run, '_prepare_workspace', prepare)
    run._fresh_workspace(job['id'], 'A')
    archived = list((store.evidence_dir(job['id']) / 'discarded-workspaces').glob('a-*/partial.txt'))
    assert len(archived) == 1 and archived[0].read_text(encoding='utf-8') == '失败现场'
    assert not (work / 'partial.txt').exists()
    side = store.get_job(job['id'])['sides']['A']
    assert not any(side[k] for k in ('video_local', 'video_url', 'demo', 'completion_recovery', 'resumed_session_id'))


def test_clean_retry_after_restart_preserves_attempt_evidence(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    store.save_settings({'codex_side_max_attempts': 2})
    job = make_job(store, tmp_path)
    evidence = store.evidence_dir(job['id']) / 'attempts'
    evidence.mkdir()
    old = evidence / 'a-01-stream.jsonl'
    old.write_text('retained raw failed attempt', encoding='utf-8')
    store.update_side(job['id'], 'A', {'attempts': [{'attempt': 1, 'status': 'cut'}]})
    run = runner.PairRunner(store)
    calls = []
    monkeypatch.setattr(run, '_fresh_workspace', lambda *a, **k: calls.append('fresh'))
    monkeypatch.setattr(runner.time, 'sleep', lambda _: None)
    def attempt(*args, **kwargs):
        calls.append(kwargs['attempt'])
        return {'code': 1, 'new_session': None, 'completed': False, 'aborted': False,
                'summary': 'failed', 'failure': 'stream disconnected', 'retryable': True}
    monkeypatch.setattr(run, '_run_attempt', attempt)
    run.run_side(job['id'], 'A')
    run.run_side(job['id'], 'A')
    assert calls == ['fresh', 2]
    assert old.read_text(encoding='utf-8') == 'retained raw failed attempt'
    assert [row['attempt'] for row in store.get_job(job['id'])['sides']['A']['attempts']] == [1, 2]


def test_response_only_continuation_blocks_single_turn(tmp_path):
    from test_codex_desk import write_rollout
    from agent_trace_kit import engines
    from agent_trace_kit.checklist import run_checklist
    store = DeskStore(tmp_path / 'desk')
    job = store.create_job({'prompt': 'original', 'agent': 'codex'})
    trace = write_rollout(tmp_path / 'codex', tmp_path, prompt='original')
    with trace.open('a', encoding='utf-8') as out:
        out.write(json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'continue'}]}}) + '\n')
    assert engines.codex_evidence(trace)['users'] == ['original', 'continue']
    job['sides']['A'].update(jsonl_local=str(trace), session_id='session-a')
    check = next(x for x in run_checklist(job)['items'] if x['id'] == 'A_single_turn')
    assert check['blocking'] and not check['ok']


def test_identical_prompt_across_distinct_turns_cannot_merge(tmp_path):
    from agent_trace_kit.engines import codex_evidence
    rows = [
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'one'}},
        {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'same'}},
        {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'two'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': 'same'}]}},
    ]
    path = tmp_path / 'trace.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
    assert codex_evidence(path)['user_turn_count'] == 2

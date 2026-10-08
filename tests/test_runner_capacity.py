"""Admission limits apply to existing workers without cancelling live jobs."""
import threading

from agent_trace_kit.desk_store import DeskStore
from agent_trace_kit.runner import PairRunner


def test_lower_limit_drains_live_pairs_before_admitting_more(tmp_path, monkeypatch):
    store = DeskStore(tmp_path / 'desk')
    store.save_settings({'max_parallel_pairs': 3})
    runner = PairRunner(store)
    jobs = [store.create_job({'prompt': 'fixture'})['id'] for _ in range(5)]
    for ident in jobs:
        for side in ('A', 'B'):
            store.update_side(ident, side, {'status': 'done'})
    entered = {ident: threading.Event() for ident in jobs}
    release = {ident: threading.Event() for ident in jobs}
    finished = {ident: threading.Event() for ident in jobs}

    def execute(ident):
        entered[ident].set()
        release[ident].wait(5)
        finished[ident].set()

    monkeypatch.setattr(runner, '_run_job', execute)
    runner.ensure_workers()
    try:
        for ident in jobs[:3]:
            runner.enqueue(ident)
        assert all(entered[ident].wait(2) for ident in jobs[:3])
        store.save_settings({'max_parallel_pairs': 1})
        runner.ensure_workers()
        for ident in jobs[3:]:
            runner.enqueue(ident)
        release[jobs[0]].set()
        assert finished[jobs[0]].wait(1)
        assert not entered[jobs[3]].wait(0.4), 'Third pair admitted with a limit of one'
        assert not entered[jobs[4]].is_set()
        assert not finished[jobs[1]].is_set(), 'Changing the cap cancelled a live pair'
        release[jobs[1]].set()
        assert finished[jobs[1]].wait(1)
        assert not entered[jobs[3]].wait(0.4), 'Second pair admitted with a limit of one'
        release[jobs[2]].set()
        assert entered[jobs[3]].wait(2)
        assert not entered[jobs[4]].wait(0.4)
        store.save_settings({'max_parallel_pairs': 2})
        runner.ensure_workers()
        assert entered[jobs[4]].wait(2), 'Raising the cap did not admit queued work'
    finally:
        runner.stop()
        for event in release.values():
            event.set()
        for worker in runner._workers:
            worker.join(3)
    assert not any(worker.is_alive() for worker in runner._workers)

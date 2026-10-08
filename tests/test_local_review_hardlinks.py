import io
import json
from pathlib import Path
import tarfile
import time
import os

import pytest

from agent_trace_kit import local_review_worker as worker
from agent_trace_kit.batch_pipeline import atomic_json, sha256

REAL_PUBLISH = worker.publish

pytestmark = pytest.mark.usefixtures("configured_review_worker")


def archive(path, entries):
    with tarfile.open(path, 'w:gz') as out:
        for name, content, kind, link in entries:
            item = tarfile.TarInfo(name)
            item.type, item.linkname = kind, link
            item.size = len(content) if kind == tarfile.REGTYPE else 0
            out.addfile(item, io.BytesIO(content) if kind == tarfile.REGTYPE else None)


def test_hardlinks_materialized_as_independent_bytes(tmp_path):
    bundle, folder = tmp_path / 'b.tgz', tmp_path / 'workspace'
    folder.mkdir()
    archive(bundle, [('A/target/a', '真实数据'.encode('utf-8'), tarfile.REGTYPE, ''),
                     ('A/target/b', b'', tarfile.LNKTYPE, 'A/target/a')])
    worker.extract_bundle(bundle, folder)
    assert (folder / 'A/target/a').read_bytes() == (folder / 'A/target/b').read_bytes()
    (folder / 'A/target/b').write_bytes(b'changed copy')
    assert (folder / 'A/target/a').read_bytes() == '真实数据'.encode('utf-8')


@pytest.mark.parametrize('target', ['/etc/a', '../a', 'A/../a', 'A\\a', 'C:/a', 'missing', 'alias'])
def test_hardlink_unsafe_or_nonregular_targets_fail_before_any_write(tmp_path, target):
    bundle, folder = tmp_path / 'b.tgz', tmp_path / 'workspace'
    folder.mkdir()
    archive(bundle, [('source', b'ok', tarfile.REGTYPE, ''), ('alias', b'', tarfile.LNKTYPE, 'source'),
                     ('bad', b'', tarfile.LNKTYPE, target)])
    with pytest.raises(ValueError):
        worker.extract_bundle(bundle, folder)
    assert list(folder.iterdir()) == []


def test_hardlink_to_symlink_is_rejected(tmp_path):
    bundle, folder = tmp_path / 'b.tgz', tmp_path / 'workspace'
    folder.mkdir()
    archive(bundle, [('link', b'', tarfile.SYMTYPE, '/etc/private'), ('copy', b'', tarfile.LNKTYPE, 'link')])
    with pytest.raises(ValueError):
        worker.extract_bundle(bundle, folder)
    assert list(folder.iterdir()) == []


def test_hardlink_expansion_counts_toward_size_budget(tmp_path, monkeypatch):
    source = tarfile.TarInfo('source')
    source.size = 300 * 1024 * 1024
    alias = tarfile.TarInfo('alias')
    alias.type, alias.linkname = tarfile.LNKTYPE, 'source'
    class FakeArchive:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def getmembers(self): return [source, alias]
        def extractfile(self, item): pytest.fail('oversized archive should not extract')
    monkeypatch.setattr(worker.tarfile, 'open', lambda *args: FakeArchive())
    with pytest.raises(ValueError, match='too large'):
        worker.extract_bundle(tmp_path / 'unused', tmp_path)


def recovery_claim(tmp_path):
    job, run = 'pair-2222222222', '20261001-155019-930739b3'
    req = {'schema': 1, 'job': job, 'run_id': run, 'phase': 'evaluate', 'id': f'{job}-{run}-evaluate',
           'model': worker.MODEL, 'authorization': {'id': worker.AUTHORIZATION, 'model': worker.MODEL,
            'generation_model': 'auto_model/urm', 'origin': 'human-authorized-local-evaluation'},
           'workspace': f'{worker.REMOTE_EVIDENCE}/{job}/auto-review/{run}',
           'directory': f'{worker.REMOTE_EVIDENCE}/{job}/auto-review/{run}/evaluate',
           'schema_sha256': 'a' * 64, 'prompt_sha256': 'b' * 64, 'bundle_sha256': 'c' * 64,
           'bundle_size': 100, 'deadline': time.time() + 300, 'images': []}
    directory = tmp_path / req['id']
    directory.mkdir()
    (directory / 'workspace').mkdir()
    (directory / 'workspace.tar.gz').write_bytes(b'bundle')
    atomic_json(directory / 'request.json', req)
    receipt = {'schema': 1, 'kind': 'verified-prelaunch-hardlink-extraction-failure',
               'job': job, 'run_id': run, 'phase': 'evaluate', 'request_id': req['id'],
               'legacy_worker_sha256': 'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
               'request_sha256': sha256(directory / 'request.json'), 'bundle_sha256': req['bundle_sha256'],
               'cli_dispatched': False}
    atomic_json(directory / 'prelaunch-recovery.json', receipt)
    worker.RECOVERY_AUTHORIZATIONS[req['id']] = dict(receipt)
    return req, directory, receipt


@pytest.mark.parametrize('filename', ['process.json', 'events.jsonl', 'final.json', 'dispatch-intent.json', 'schema.json', 'prompt.txt'])
def test_claim_after_possible_dispatch_never_recovers(tmp_path, filename, monkeypatch):
    req, directory, _ = recovery_claim(tmp_path)
    (directory / filename).write_text('{}', encoding='utf-8')
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('duplicate dispatch'))
    with pytest.raises(RuntimeError, match='Prelaunch recovery'):
        worker.execute(req, tmp_path)


@pytest.mark.parametrize('key,value', [('request_sha256', 'changed'), ('bundle_sha256', 'changed'),
                                     ('legacy_worker_sha256', 'changed'), ('cli_dispatched', True), ('run_id', 'other')])
def test_recovery_receipt_drift_refused(tmp_path, key, value):
    req, directory, receipt = recovery_claim(tmp_path)
    receipt[key] = value
    atomic_json(directory / 'prelaunch-recovery.json', receipt)
    with pytest.raises(RuntimeError): worker.validate_prelaunch_recovery(directory, req)


def test_expired_prelaunch_keeps_original_deadline(tmp_path, monkeypatch):
    req, directory, receipt = recovery_claim(tmp_path)
    req['deadline'] = time.time() - 1
    atomic_json(directory / 'request.json', req)
    receipt['request_sha256'] = sha256(directory / 'request.json')
    atomic_json(directory / 'prelaunch-recovery.json', receipt)
    worker.RECOVERY_AUTHORIZATIONS[req['id']] = dict(receipt)
    monkeypatch.setattr(worker.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('expired request dispatch'))
    with pytest.raises(ValueError, match='deadline'):
        worker.execute(req, tmp_path)


def test_nonempty_prelaunch_workspace_refused(tmp_path):
    req, directory, _ = recovery_claim(tmp_path)
    (directory / 'workspace' / 'partial').write_bytes(b'original')
    with pytest.raises(RuntimeError): worker.validate_prelaunch_recovery(directory, req)


def test_dispatch_intent_exclusive_and_unchanged(tmp_path):
    req, directory, _ = recovery_claim(tmp_path)
    worker.reserve_dispatch(directory, req)
    before = (directory / 'dispatch-intent.json').read_bytes()
    with pytest.raises(FileExistsError):
        worker.reserve_dispatch(directory, req)
    assert (directory / 'dispatch-intent.json').read_bytes() == before


@pytest.mark.skipif(os.name != 'nt', reason='Real Windows extended paths')
def test_windows_long_build_output_path(tmp_path):
    bundle, folder = tmp_path / 'b.tgz', tmp_path / 'workspace'
    folder.mkdir()
    name = 'A/target/' + '/'.join(['deepdirectory' * 3] * 5) + '/out.bin'
    archive(bundle, [(name, b'long-path-bytes', tarfile.REGTYPE, '')])
    worker.extract_bundle(bundle, folder)
    target = Path('\\\\?\\' + str((folder / name).absolute()))
    assert target.read_bytes() == b'long-path-bytes'


@pytest.mark.parametrize('names', [['a', './a'], ['a', 'a/b'], ['x/y', 'x'], ['a', 'a//b']])
def test_conflicting_destinations_fail_before_writing(tmp_path, names):
    bundle, folder = tmp_path / 'b.tgz', tmp_path / 'workspace'
    folder.mkdir()
    archive(bundle, [(name, b'bytes', tarfile.REGTYPE, '') for name in names])
    with pytest.raises(ValueError): worker.extract_bundle(bundle, folder)
    assert list(folder.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows destination rules')
@pytest.mark.parametrize('names', [['a', 'A'], ['a', 'a.'], ['NUL'], ['a/CON.txt']])
def test_windows_alias_or_device_rejected(tmp_path, names):
    bundle, folder = tmp_path / 'b.tgz', tmp_path / 'workspace'
    folder.mkdir()
    archive(bundle, [(name, b'bytes', tarfile.REGTYPE, '') for name in names])
    with pytest.raises(ValueError): worker.extract_bundle(bundle, folder)
    assert list(folder.iterdir()) == []


def test_same_prelaunch_claim_dispatches_once_and_binds_proof(tmp_path, monkeypatch):
    req, directory, receipt = recovery_claim(tmp_path)
    bundle = directory / 'workspace.tar.gz'
    schema, prompt = b'{"type":"object"}', '保持原预算'.encode('utf-8')
    archive(bundle, [('evaluate/schema.json', schema, tarfile.REGTYPE, ''),
                     ('evaluate/prompt.txt', prompt, tarfile.REGTYPE, ''),
                     ('source-manifest.json', b'{"files":{"A":{},"B":{}}}', tarfile.REGTYPE, ''),
                     ('source', b'bytes', tarfile.REGTYPE, ''), ('copy', b'', tarfile.LNKTYPE, 'source')])
    import hashlib
    req.update(bundle_sha256=sha256(bundle), bundle_size=bundle.stat().st_size,
               schema_sha256=hashlib.sha256(schema).hexdigest(), prompt_sha256=hashlib.sha256(prompt).hexdigest())
    atomic_json(directory / 'request.json', req)
    receipt.update(request_sha256=sha256(directory / 'request.json'), bundle_sha256=req['bundle_sha256'])
    atomic_json(directory / 'prelaunch-recovery.json', receipt)
    worker.RECOVERY_AUTHORIZATIONS[req['id']] = dict(receipt)
    dispatches, publications = [], []
    monkeypatch.setattr(worker.shutil, 'which', lambda executable: 'codex')
    monkeypatch.setattr(worker.subprocess, 'check_output', lambda *args, **kwargs: 'codex-cli 0.159.3')
    monkeypatch.setattr(worker.subprocess, 'run', lambda *args, **kwargs: pytest.fail('No duplicate bundle transport'))
    class Proc:
        pid, returncode = 12345, 0
        def communicate(self, content, timeout):
            assert content == prompt.decode('utf-8') and 0 < timeout <= 300
    def popen(command, **kwargs):
        assert (directory / 'dispatch-intent.json').exists()
        dispatches.append(command)
        kwargs['stdout'].write(json.dumps({'type': 'thread.started', 'thread_id': 'unit-test'}) + '\n')
        kwargs['stdout'].write(json.dumps({'type': 'turn.completed'}) + '\n')
        (directory / 'final.json').write_text('{}', encoding='utf-8')
        return Proc()
    monkeypatch.setattr(worker.subprocess, 'Popen', popen)
    monkeypatch.setattr(worker, 'publish', lambda directory, request: publications.append(request))
    worker.execute(req, tmp_path)
    worker.execute(req, tmp_path)
    assert len(dispatches) == 1 and len(publications) == 2
    process = json.loads((directory / 'process.json').read_text(encoding='utf-8'))
    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
    assert process['prelaunch_recovery_sha256'] == result['prelaunch_recovery_sha256'] == sha256(directory / 'prelaunch-recovery.json')
    assert process['dispatch_intent_sha256'] == result['dispatch_intent_sha256'] == sha256(directory / 'dispatch-intent.json')
    assert result['process_sha256'] == sha256(directory / 'process.json')


@pytest.mark.parametrize('filename', ['dispatch-intent.json', 'prelaunch-recovery.json'])
def test_changed_dispatch_proof_cannot_publish(tmp_path, filename, monkeypatch):
    # Build the successful fixture, then use the real publisher's preflight.
    test_same_prelaunch_claim_dispatches_once_and_binds_proof(tmp_path, monkeypatch)
    from agent_trace_kit.local_review_relay import LocalReviewPipeline
    directory = next(tmp_path.glob('pair-*'))
    req = json.loads((directory / 'request.json').read_text(encoding='utf-8'))
    (directory / filename).write_text('{}', encoding='utf-8')
    monkeypatch.setattr(LocalReviewPipeline, '_validate_terminal', lambda *args: None)
    monkeypatch.setattr(worker, 'remote', lambda *args, **kwargs: pytest.fail('Changed provenance must not publish'))
    # Restore the real function replaced by the successful mock fixture.
    with pytest.raises(RuntimeError, match='provenance'):
        REAL_PUBLISH(directory, req)


def test_deadline_expires_during_preparation_no_cli_dispatch(tmp_path, monkeypatch):
    original = worker.validate_request
    calls = []
    original_time = time.time
    def validate(req, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            monkeypatch.setattr(worker.time, 'time', lambda: original_time() + 1000)
        return original(req, **kwargs)
    monkeypatch.setattr(worker, 'validate_request', validate)
    with pytest.raises(ValueError, match='deadline'):
        test_same_prelaunch_claim_dispatches_once_and_binds_proof(tmp_path, monkeypatch)
    directory = next(tmp_path.glob('pair-*'))
    assert not (directory / 'dispatch-intent.json').exists()
    assert not (directory / 'process.json').exists() and not (directory / 'events.jsonl').exists()

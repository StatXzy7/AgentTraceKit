"""Readable presentation must finish before the recorder's completion receipt."""
import io
import json
import sys
import os
import pytest

from agent_trace_kit import demo_terminal


def test_command_is_visible_before_process_launch_and_result_after_hold(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONUTF8', '0')
    monkeypatch.setenv('PYTHONIOENCODING', 'ascii')
    result = tmp_path / 'process.json'
    spec = {'command': [sys.executable, '-c', 'print("RESULT-真实输出")'],
            'cwd': str(tmp_path), 'result': str(result), 'timeout': 10,
            'presentation_timeout': 100, 'readable': True}
    clock, stream, stages = [0.0], io.StringIO(), []
    def wait(seconds):
        assert not result.exists(), 'Receipt would let FFmpeg stop before reading ends'
        clock[0] += seconds
        stages.append(stream.getvalue())
    demo_terminal.terminal(spec, stream=stream, wait=wait, clock=lambda: clock[0])
    receipt = json.loads(result.read_text(encoding='utf-8'))
    assert receipt['exit_code'] == 0 and receipt['presentation_complete']
    assert receipt['presentation_duration_seconds'] >= 30
    assert 'RESULT-真实输出' in (tmp_path / 'terminal-output.log').read_text(encoding='utf-8')
    assert any('$ ' in s and 'exit 0' not in s for s in stages)
    assert any('exit 0' in s for s in stages)


def test_pagination_preserves_every_line_and_wraps_wide_text():
    clock, stream = [0.0], io.StringIO()
    waits = []
    def wait(seconds):
        waits.append(seconds)
        clock[0] += seconds
    view = demo_terminal.Presentation({}, stream=stream, wait=wait, clock=lambda: clock[0])
    for n in range(70):
        view.line(f'ROW-{n:03d}')
    assert view.pages >= 3
    assert waits.count(6) >= 2
    assert all(f'ROW-{n:03d}' in stream.getvalue() for n in range(70))
    rows = demo_terminal.wrap_display('中' * 110, 104)
    assert ''.join(rows) == '中' * 110
    assert all(demo_terminal.cell_width(row) <= 104 for row in rows)


def test_timeout_cannot_report_complete_presentation(tmp_path):
    clock = [0.0]
    def wait(seconds):
        clock[0] += seconds
    result = tmp_path / 'process.json'
    demo_terminal.terminal({'command': [sys.executable, '-c', 'print("too much")'],
                            'cwd': str(tmp_path), 'result': str(result), 'timeout': 10,
                            'presentation_timeout': 0.1, 'readable': True},
                           stream=io.StringIO(), wait=wait, clock=lambda: clock[0])
    receipt = json.loads(result.read_text(encoding='utf-8'))
    assert receipt['presentation_complete'] is False
    assert receipt['error']


def test_recording_limit_clamps_legacy_and_api_settings(tmp_path):
    from agent_trace_kit.desk_store import DeskStore
    store = DeskStore(tmp_path)
    store.settings_path.write_text('{"video_max_seconds":600}', encoding='utf-8')
    assert store.settings()['video_max_seconds'] == 89
    assert store.save_settings({'video_max_seconds': 999})['video_max_seconds'] == 89
    assert json.loads(store.settings_path.read_text(encoding='utf-8'))['video_max_seconds'] == 89


@pytest.mark.skipif(sys.platform == 'win32', reason='Linux owned process-group cleanup')
def test_exited_parent_with_inherited_pipe_is_cleaned(tmp_path):
    result = tmp_path / 'process.json'
    child = 'import time; time.sleep(60)'
    command = [sys.executable, '-c',
               f'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c",{child!r}]); print(p.pid,flush=True)']
    demo_terminal.terminal({'command': command, 'cwd': str(tmp_path), 'result': str(result),
                            'timeout': 0.3, 'presentation_timeout': 10, 'readable': False},
                           stream=io.StringIO(), wait=lambda _: None)
    receipt = json.loads(result.read_text(encoding='utf-8'))
    assert receipt['timed_out'] is True
    pid = int((tmp_path / 'terminal-output.log').read_text(encoding='utf-8').strip())
    stat = __import__('pathlib').Path(f'/proc/{pid}/stat')
    assert not stat.exists() or stat.read_text().split(')')[1].split()[0] == 'Z'

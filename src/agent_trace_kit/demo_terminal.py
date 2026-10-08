"""Paced presentation of real command output, with an unmodified raw log."""
from __future__ import annotations

import codecs
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path

from . import procmon
from .desk_store import DeskStore

_ESCAPES = re.compile(r'\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]')


def cell_width(text: str) -> int:
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def wrap_display(text: str, columns: int) -> list[str]:
    """Wrap displayed text only; raw subprocess bytes stay unchanged on disk."""
    text = _ESCAPES.sub('', text).expandtabs(4)
    text = ''.join(c for c in text if c >= ' ' or c == '\t')
    rows, row, width = [], '', 0
    for char in text:
        size = cell_width(char)
        if row and width + size > columns:
            rows.append(row)
            row, width = '', 0
        row += char
        width += size
    return [*rows, row]


class Presentation:
    def __init__(self, spec: dict, *, stream=None, wait=time.sleep, clock=time.monotonic):
        self.stream = stream if stream is not None else sys.stdout
        self.wait, self.clock = wait, clock
        self.started = clock()
        budget = min(79, float(spec.get('presentation_timeout', 79)))
        self.deadline = self.started + budget
        self.readable = spec.get('readable', True)
        self.rate = max(15, min(120, float(spec.get('chars_per_second', 40))))
        self.command_pause = max(2, min(15, float(spec.get('command_pause', 5))))
        self.page_pause = max(3, min(15, float(spec.get('page_pause', 6))))
        self.final_pause = max(8, min(30, float(spec.get('final_pause', 12))))
        self.min_seconds = min(max(0, budget - 2), max(20, float(spec.get('min_seconds', 30))))
        self.rows, self.lines, self.pages = 0, 0, 1

    def pause(self, seconds: float) -> None:
        if self.clock() + seconds > self.deadline:
            raise TimeoutError('输出尚未完整展示，达到录屏时限；原始输出见 terminal-output.log')
        self.wait(seconds)

    def emit(self, text: str) -> None:
        self.stream.write(text)
        self.stream.flush()

    def command(self, command: list[str]) -> None:
        self.emit('Automated demo | real output, paced for reading\n\n')
        self.rows = 2
        text = '$ ' + shlex.join(command)
        for row in wrap_display(text, 104):
            # Automated typing is presentation, not a claim of human input.
            for char in row:
                self.emit(char)
                if self.readable:
                    self.pause(0.02)
            self.emit('\n')
            self.rows += 1
        self.pause(self.command_pause if self.readable else 2)
        self.emit('\n')
        self.rows += 1

    def line(self, text: str) -> None:
        for row in wrap_display(text, 104):
            if self.readable and self.rows >= 23:
                self.emit('\n--- Page complete; pause before next page ---\n')
                self.pause(self.page_pause)
                self.pages += 1
                self.emit(f'\x1b[2J\x1b[HOutput | page {self.pages}\n\n')
                self.rows = 2
            self.emit(row + '\n')
            self.rows += 1
            self.lines += 1
            if self.readable:
                self.pause(max(0.25, cell_width(row) / self.rate))

    def finish(self, code: int, timed_out: bool) -> None:
        self.line(f'[exit {code}; timeout={timed_out}]')
        if self.readable:
            self.pause(max(self.final_pause, self.min_seconds - (self.clock() - self.started)))


def terminal(spec: dict, *, stream=None, wait=time.sleep, clock=time.monotonic) -> None:
    """Drain the process independently; publish completion only after reading time."""
    from .demo import argv
    result = Path(spec['result'])
    raw_path = result.parent / 'terminal-output.log'
    command = argv(spec['command'])
    view = Presentation(spec, stream=stream, wait=wait, clock=clock)
    done, timed_out = threading.Event(), threading.Event()
    process = timer = reader = None
    errors = []
    cleanup_lock = threading.Lock()
    cleaned = False
    def cleanup():
        nonlocal cleaned
        with cleanup_lock:
            if process is not None and not cleaned:
                procmon.kill_tree(process.pid)
                cleaned = True
    state = {'exit_code': None, 'timed_out': False, 'presentation_complete': False}
    try:
        view.command(command)
        process = subprocess.Popen(command, cwd=spec['cwd'], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT,
                                   env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
                                   **procmon.hidden_console_kwargs())
        def timeout():
            if not done.is_set():
                timed_out.set()
                cleanup()
        timer = threading.Timer(float(spec['timeout']), timeout)
        timer.start()
        raw_path.touch()
        def capture():
            try:
                with raw_path.open('wb') as output:
                    while chunk := process.stdout.read1(4096):
                        output.write(chunk)
                        output.flush()
                state['exit_code'] = process.wait()
            except Exception as exc:
                errors.append(str(exc))
            finally:
                timer.cancel()
                cleanup()
                done.set()
        reader = threading.Thread(target=capture, daemon=True)
        reader.start()
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        pending = ''
        with raw_path.open('rb') as output:
            while True:
                chunk = output.read(4096)
                if not chunk:
                    if done.is_set():
                        # Re-read after producer completion to close the EOF race.
                        chunk = output.read(4096)
                        if not chunk:
                            pending += decoder.decode(b'', final=True)
                            break
                    else:
                        if clock() >= view.deadline:
                            raise TimeoutError('演示进程或输出展示超过录屏时限')
                        done.wait(0.03)
                        continue
                pending += decoder.decode(chunk).replace('\r\n', '\n').replace('\r', '\n')
                while '\n' in pending or len(pending) > 2048:
                    if '\n' in pending:
                        line, pending = pending.split('\n', 1)
                    else:
                        line, pending = pending[:1024], pending[1024:]
                    view.line(line)
        if pending:
            view.line(pending)
        if errors:
            raise RuntimeError('; '.join(errors))
        state['timed_out'] = timed_out.is_set()
        view.finish(state['exit_code'], state['timed_out'])
        state['presentation_complete'] = True
    except Exception as exc:
        state['error'] = f'{type(exc).__name__}: {exc}'
        view.emit('\n[INCOMPLETE PRESENTATION] ' + str(exc) + '\n')
    finally:
        if timer:
            timer.cancel()
        cleanup()
        if process:
            process.wait(timeout=10)
        if reader:
            reader.join(timeout=5)
            if reader.is_alive():
                state.update(presentation_complete=False, error='输出管道未完成清理')
        state.update(timed_out=timed_out.is_set(), display_lines=view.lines, pages=view.pages,
                     output_bytes=raw_path.stat().st_size if raw_path.exists() else 0,
                     presentation_duration_seconds=round(clock() - view.started, 2))
        DeskStore._atomic_write_json(result, state)

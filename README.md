# AgentTraceKit

> One command to turn your coding-agent session into a verifiable trajectory bundle.

No database. No proxy. No backend. No JSONL knowledge required.

## Download → Run → Choose → Done

```powershell
pipx install agent-trace-kit
atk
```

Or use `uv tool install agent-trace-kit`. `atk doctor` checks your local setup. `atk collect --latest` selects the newest Codex CLI session; `atk collect --input FILE` handles an explicit file. Bundles are local and include untouched raw bytes, normalized events, evidence, an annotation template, an offline HTML timeline, and independent verification. `atk verify BUNDLE` checks hashes and references. `atk open` opens the latest report.

AgentTraceKit v0.4.0 supports **Codex CLI and Claude Code** imports. Observe collection is read-only; managed and WSL execution are explicit modes. Pair drafts, human GSB review, strict CSV export, and Windows PowerShell entry points are included. This tool does not upload data or collect telemetry. Review raw trajectories before sharing them.

For the Windows collector workflow see [Windows Quickstart](docs/WINDOWS_QUICKSTART.md), [Configuration](docs/CONFIGURATION.md), and [Pairwise Workflow](docs/PAIRWISE_WORKFLOW.md). `atk demo --synthetic` creates two local fake sessions and a pair export without starting a real model or reading credentials.

## Pair Desk (local A/B runner)

There is **no** `Pair Desk` exe or folder at the repo root. That is expected. The desk is this package started as a local app, not a separate install.

```powershell
cd D:\myprojects\AgentTraceKit
python -m pip install -e .
python -m agent_trace_kit.desk          # tray window + http://127.0.0.1:8765
python -m agent_trace_kit.desk status
python -m agent_trace_kit.desk stop
```

Or double-click `scripts\windows\start-desk.vbs`. Closing the browser does not stop the desk; close the taskbar window or run `stop`.

| What you want | Where it actually is |
| --- | --- |
| Source | `src/agent_trace_kit/desk.py`, `desk_app.py`, `desk_service.py`, `desk_store.py` |
| Start script | `scripts/windows/start-desk.vbs` |
| Jobs, A/B workspaces, evidence, lock | `C:\AgentTraceKit-data\desk\` (outside git; override with `ATK_DESK_HOME`) |
| Operator guide | [docs/PAIR_DESK.md](docs/PAIR_DESK.md) |

## Review and annotate

`atk browse` opens a local browser session picker. Choose a session and click **Collect & Review**. The resulting browser workspace includes Overview, Timeline, Evidence, Annotation, Files, and a Verify button, so normal review requires no further command-line steps. `atk view BUNDLE` opens the same workspace directly. `atk annotate BUNDLE` remains available for opening the standalone annotation workbench. The raw session remains unchanged. To index a directory containing bundles for later search, run `atk corpus index DIRECTORY`; this creates a rebuildable local SQLite cache.

### Bundle files

`raw/` is the original JSONL byte-for-byte. `timeline.html` opens offline. `trajectory/` contains normalized events and interactions. `evidence/evidence.csv` maps events to source lines. `annotation/annotation_template.csv` leaves judgment fields for a human reviewer. `manifest.json` and `validation.json` record integrity and parse status.

AgentTraceKit focuses on one-command collection and verifiable evidence. For cross-agent search consider clustervision; for HTTP/WebSocket debugging consider claude-tap.

## Development

```powershell
python -m pip install -e .
python -m pytest
```

MIT licensed. Never publish a raw agent trajectory without reviewing it first.

# AgentTraceKit

> One command to turn your coding-agent session into a verifiable trajectory bundle.

No database. No proxy. No backend. No JSONL knowledge required.

## Download → Run → Choose → Done

```powershell
pipx install agent-trace-kit
atk
```

Or use `uv tool install agent-trace-kit`. `atk doctor` checks your local setup. `atk collect --latest` selects the newest Codex CLI session; `atk collect --input FILE` handles an explicit file. Bundles are local and include untouched raw bytes, normalized events, evidence, an annotation template, an offline HTML timeline, and independent verification. `atk verify BUNDLE` checks hashes and references. `atk open` opens the latest report.

AgentTraceKit v0.1.0 supports **Codex CLI**. Claude Code, ZCode, and other agents are planned. This tool does not upload data or collect telemetry. Review raw trajectories before sharing them.

### Bundle files

`raw/` is the original JSONL byte-for-byte. `timeline.html` opens offline. `trajectory/` contains normalized events and interactions. `evidence/evidence.csv` maps events to source lines. `annotation/annotation_template.csv` leaves judgment fields for a human reviewer. `manifest.json` and `validation.json` record integrity and parse status.

AgentTraceKit focuses on one-command collection and verifiable evidence. For cross-agent search consider clustervision; for HTTP/WebSocket debugging consider claude-tap.

## Development

```powershell
python -m pip install -e .
python -m pytest
```

MIT licensed. Never publish a raw agent trajectory without reviewing it first.

# AgentTraceKit

Turn Codex CLI and Claude Code sessions into verifiable trajectory bundles, and run comparable A/B coding tasks from a browser.

[中文说明](README.zh-CN.md) · [Usability and validation](docs/USABILITY_AND_IMPROVEMENTS.md) · [Architecture](docs/ARCHITECTURE.md) · [Changelog](CHANGELOG.md)

## Install the current main

Python 3.10+ is required; Python 3.11+ is recommended for inherited Codex TOML configuration. Install Git. Pair generation additionally needs the selected CLI and its authentication; GitHub provisioning needs `gh` and `gh auth login`.

```sh
git clone https://github.com/StatXzy7/AgentTraceKit.git
cd AgentTraceKit
python -m pip install -e .
atk doctor
atk browse
```

This installs this repository's version. A GitHub update does not publish a new PyPI package. For isolated installation, use `uv tool install git+https://github.com/StatXzy7/AgentTraceKit.git@main`.

The collector reads local files without uploading them. `atk collect --latest` selects the latest Codex session; `atk collect --input FILE` accepts an explicit session. `atk verify BUNDLE` checks hashes and evidence references; `atk view BUNDLE` opens the review workspace. `atk demo --synthetic` exercises collection and export without a model or credentials. Raw bytes remain intact; inspect them before sharing.

## Pair Desk

```sh
python -m agent_trace_kit.desk
python -m agent_trace_kit.desk status
python -m agent_trace_kit.desk stop
```

Open <http://127.0.0.1:8765/>. Windows also provides `scripts/windows/start-desk.vbs`; Linux can use foreground mode or the supplied systemd templates. Closing a browser does not stop the service.

Choose one CLI for both sides, a model and connection, and a baseline: a local standalone Git clone without submodules, an existing GitHub commit, or a newly provisioned GitHub repository. A/B and retries use the frozen full SHA and independent branches. Each Pair binds its connection revision and model. Dedicated keys are write-only in the UI, DPAPI-protected on Windows, and stored in private files on Linux; global CLI configuration is not rewritten.

The runner isolates CLI configuration, disables extra agent dispatch, retains every attempt, and binds raw evidence to the expected session, workspace, prompt and completion. Bounded request retries preserve the current session; clean retries use the original baseline. Failure evidence is retained separately. These controls are not OS-level isolation between A/B.

Linux can demonstrate committed products in a separate copy, record X11 with FFmpeg, and export logs/reports/video. Recording has an **89-second hard cap**; a successful configured demo is not complete product acceptance. Human review remains the default. An explicitly authorized AI evaluation workflow keeps its source visible and cannot fill human-only validity or no-AI declarations.

Strict export keeps the established **26-column TSV** format and checks evidence, baseline/model consistency and review state. See [Pair Desk](docs/PAIR_DESK.md), [single-agent controls](docs/COMPLIANCE_GUARD.md), [Linux setup](docs/LINUX_DEPLOYMENT.md), and [Linux demos](docs/LINUX_DEMO.md).

## Development

```sh
python -m pip install -e . pytest
python -m pytest -q tests
```

CI runs the framework tests on Windows and Ubuntu with Python 3.11. Tests use synthetic evidence and mocks; they do not prove live provider availability or complete GUI acceptance. See [validation boundaries and improvements](docs/USABILITY_AND_IMPROVEMENTS.md).

MIT licensed. Keep credentials, raw private trajectories and runtime state outside Git.

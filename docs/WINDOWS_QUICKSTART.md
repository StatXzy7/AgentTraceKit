# Windows Quickstart

Use Python 3.11+ from PowerShell. `python -m pip install -e .` installs only AgentTraceKit.

`atk config init --path config.toml` creates a local configuration. `atk config validate`, `show`, and `explain data_root` validate and explain effective values. `atk collect --provider codex --input path\rollout.jsonl` (or `--provider claude`) creates an immutable raw copy and normalized evidence bundle.

For pairwise work, create a draft with `atk pair create --root data\pairs --task demo --prompt "..." --provider codex --difficulty 困难`, bind two distinct bundles with `pair bind`, import a human-written `review.toml`, then run `pair export --draft` or `--strict`. Strict export returns a non-zero exit code and a machine-readable missing list when evidence or review is incomplete.

`observe` is read-only. `managed` and WSL are explicit configuration choices and are not silently enabled. `import` accepts historical files without inventing current runtime facts. Missing permissions, locked or growing files, unavailable clients, recordings, or upload backends remain visible as incomplete/unsupported evidence.

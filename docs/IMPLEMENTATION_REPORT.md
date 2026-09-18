# Implementation report

Implemented in this repository: strict UTF-8 TOML configuration and redacted explanation; Codex and Claude observe/import adapters; immutable raw bundle collection with stable event IDs and unknown/malformed event retention; pairwise policy identifier `pairwise_gsb_20260916`; distinct A/B binding; human review import; draft/strict UTF-8-BOM CSV export; synthetic offline demo; and Windows PowerShell/CMD wrappers.

Validation run locally: `python -m pytest -q` (6 passed), `python -m compileall -q src`, `python -m pip install -e . --no-deps`, `atk config init/validate/show`, `atk doctor`, and `atk demo --synthetic`. The synthetic demo wrote two bundles and a pair CSV under a temporary directory.

Not verified here: real model execution, managed launch, WSL process cleanup, recording, S3-compatible upload, Windows CI across PowerShell versions, and remote publication. No real model was started, no credentials were read or exported, no global client configuration or hooks were changed, and no upload or Git push was performed.

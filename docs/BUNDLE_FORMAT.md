# Bundle format

A bundle is portable and offline. `manifest.json` records source and copied raw SHA-256, file hashes, session metadata, and counts. Normalized events use schema version 1.0 and retain provider-specific data under `data`. `evidence.csv` references physical source lines. `task_success` is always `unknown` unless an external verifier exists.

# Competitor notes (checked 2026-09-15)

- **openai/codex**: official Rust CLI; recorder persists replayable `.jsonl` rollouts under `CODEX_HOME` (usually `~/.codex/sessions/YYYY/MM/DD`). We follow the envelope (`timestamp`, `type`, `payload`) while preserving unknown records.
- **emberian/cv (clustervision)**: broad local-first search, conversion, and resurrection across 20+ harnesses. AgentTraceKit stays single-provider and evidence/bundle focused.
- **liaohch3/claude-tap**: captures Claude/API traffic and maintains a local trace database. AgentTraceKit does not proxy or inspect credentials.
- **cuteribs/agent-session-viewer**: multi-agent desktop/web viewer with token/cost extraction. Our HTML is only one output of collection.
- **mkmkkkkk/compactdiff** and **masonc15/codex-transcript-viewer**: compact/dedicated transcript presentation. We prioritize raw preservation and verification.
- **Datadog Labs trajectory**: observability/evaluation direction; outside this small local portable collector.

These notes are orientation, not claims of feature parity. Current local Codex version observed during development: `codex-cli 0.151.0`.

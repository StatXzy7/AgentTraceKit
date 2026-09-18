# Provider compatibility

Codex reads `CODEX_HOME/sessions` rollout JSONL and preserves unknown events. Claude reads `CLAUDE_CONFIG_DIR` project/session JSONL and preserves unknown events. Native CLI detection and current versions are reported by `doctor`; a fake CLI or fixture does not prove real-client compatibility. WSL and managed launching require explicit implementation/configuration and are not assumed by observe.

#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -lt 2 ]; then
  printf '%s\n' 'Usage: run_authorized_batch.sh BATCH_DIR SCORING_FILE [controller options]' >&2
  exit 2
fi
batch_dir=$1
scoring_file=$2
shift 2
export PATH="/opt/agenttracekit/runtime/bin:/opt/agenttracekit/runtime/node/bin:/opt/agenttracekit/venv/bin:$PATH"
export ATK_DESK_HOME="/var/lib/agenttracekit/desk"
export PYTHONUNBUFFERED=1
exec /opt/agenttracekit/venv/bin/python -m agent_trace_kit.batch_pipeline \
  --batch-dir "$batch_dir" --scoring-file "$scoring_file" --desk-home "$ATK_DESK_HOME" "$@"

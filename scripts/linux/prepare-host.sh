#!/usr/bin/env bash
# Alibaba Cloud Linux 3. Installs only the dedicated ATK runtime; no service start.
set -euo pipefail
test "$(id -u)" = 0
dnf install -y python3.11 python3.11-pip git-core tmux ripgrep gcc gcc-c++ make unzip tar gzip xz
id atk >/dev/null 2>&1 || useradd --create-home --shell /bin/bash atk
install -d -m 700 -o atk -g atk /home/atk/.ssh /home/atk/.codex /home/atk/.claude \
  /var/lib/agenttracekit /var/lib/agenttracekit/desk /var/lib/agenttracekit/baselines
install -d -m 755 /opt/agenttracekit/releases /opt/agenttracekit/runtime
runtime=/opt/agenttracekit/runtime
node_version=22.23.3
if [ ! -x "$runtime/node/bin/node" ]; then
  download=$(mktemp -d)
  trap 'rm -rf -- "$download"' EXIT
  cd "$download"
  curl -fsSLO --retry 3 "https://nodejs.org/dist/v$node_version/node-v$node_version-linux-x64.tar.xz"
  curl -fsSLO --retry 3 "https://nodejs.org/dist/v$node_version/SHASUMS256.txt"
  grep " node-v$node_version-linux-x64.tar.xz$" SHASUMS256.txt | sha256sum -c -
  tar -xJf "node-v$node_version-linux-x64.tar.xz" -C "$runtime"
  ln -s "node-v$node_version-linux-x64" "$runtime/node"
fi
export PATH="$runtime/node/bin:$runtime/bin:$PATH"
npm install --global --prefix "$runtime" @openai/codex@0.154.0 @anthropic-ai/claude-code@2.1.260
if [ ! -x "$runtime/bin/gh" ]; then
  download=$(mktemp -d)
  trap 'rm -rf -- "$download"' EXIT
  cd "$download"
  curl -fsSLO --retry 3 --max-time 600 https://github.com/cli/cli/releases/download/v2.101.0/gh_2.101.0_linux_amd64.tar.gz
  curl -fsSLO --retry 3 --max-time 120 https://github.com/cli/cli/releases/download/v2.101.0/gh_2.101.0_checksums.txt
  grep ' gh_2.101.0_linux_amd64.tar.gz$' gh_2.101.0_checksums.txt | sha256sum -c -
  tar -xzf gh_2.101.0_linux_amd64.tar.gz -C "$runtime"
  ln -s ../gh_2.101.0_linux_amd64/bin/gh "$runtime/bin/gh"
fi
if [ ! -x "$runtime/bin/ffprobe" ]; then
  download=$(mktemp -d)
  trap 'rm -rf -- "$download"' EXIT
  cd "$download"
  curl -fsSL --retry 3 --max-time 600 https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz -o ffmpeg.tar.xz
  printf '%s\n' 'abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67  ffmpeg.tar.xz' | sha256sum -c -
  tar -xJf ffmpeg.tar.xz -C "$runtime"
  ln -s ../ffmpeg-7.0.2-amd64-static/ffmpeg "$runtime/bin/ffmpeg"
  ln -s ../ffmpeg-7.0.2-amd64-static/ffprobe "$runtime/bin/ffprobe"
fi
python3.11 -m venv /opt/agenttracekit/venv
/opt/agenttracekit/venv/bin/pip install --upgrade pip setuptools pytest
printf 'Dedicated runtime ready: '
node --version
codex --version
claude --version

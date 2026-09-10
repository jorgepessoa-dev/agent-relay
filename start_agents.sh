#!/bin/bash
# Idempotent agent-session starter — restores the 4 agent tmux sessions after
# reboot. Skips any session that already exists (never double-starts).
#
# Invocations mirror what the live sessions use today:
#   deepcode: deepcode --resume <uuid>   (uuid from state/deepcode_resume.uuid)
#   codex:   codex resume --last
#   gemini:  .venv/bin/python scripts/ops/gemini_agent.py
#   coordinator: human-facing, NOT auto-started (set START_COORDINATOR=1).
set -eu

NVM_BIN="$HOME/.nvm/versions/node/v24.19.0/bin"
export PATH="$NVM_BIN:$PATH"
TA=/opt/tradingadvisor
UUID_FILE=/opt/agent-relay/state/deepcode_resume.uuid

start_session() {
  local name="$1"; shift
  if tmux has-session -t "$name" 2>/dev/null; then
    echo "SKIP $name (session exists)"
    return 0
  fi
  echo "START $name: $*"
  tmux new -d -s "$name" "$@"
}

# deepcode needs its resume uuid or it starts a blank session. The operator
# refreshes state/deepcode_resume.uuid whenever the session is recreated.
if [ -f "$UUID_FILE" ]; then
  UUID="$(cat "$UUID_FILE")"
  start_session deepcode bash -lc "cd /opt/agent-relay && deepcode --resume '$UUID'"
else
  echo "WARN: $UUID_FILE missing — starting blank deepcode session (thread not resumed)"
  start_session deepcode bash -lc "cd /opt/agent-relay && deepcode"
fi

start_session codex bash -lc "cd /opt/tradingadvisor && codex resume --last"
start_session gemini bash -lc "cd /opt/tradingadvisor && .venv/bin/python scripts/ops/gemini_agent.py"

if [ "${START_COORDINATOR:-0}" = "1" ]; then
  start_session claude bash -lc "cd /opt/tradingadvisor && claude --remote-control coordinator"
fi

# --- governance handshake (owner 2026-09-10: any agent that comes up is
# GOVERNED automatically, or the boot FAILS LOUD). The ack is issued by the
# BOOTSTRAP, so it is recorded with acked_by=bootstrap and only means "this
# session was started with the governance pointer and hash in place" — never
# "the agent read it".
PIN_DIR="/root"
POINTERS="/root/.claude/CLAUDE.md /root/.codex/AGENTS.md /root/.deepcode/AGENTS.md"
for ptr in $POINTERS; do
  if [ ! -s "$ptr" ]; then
    echo "GOVERNANCE FAIL: pointer missing or empty: $ptr (session will be UNGOVERNED)"
    return 1 2>/dev/null || exit 1
  fi
done
for agent in coordinator deepcode codex gemini; do
  if [ -x /opt/tradingadvisor/.venv/bin/python ]; then
    /opt/tradingadvisor/.venv/bin/python /opt/tradingadvisor/scripts/ops/gov_ack.py \
      ack --agent "$agent" --by bootstrap || true
  fi
done
echo "governance handshake: pointers present, acks issued (acked_by=bootstrap)"

echo "start_agents done"

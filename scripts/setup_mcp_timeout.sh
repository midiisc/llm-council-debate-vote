#!/usr/bin/env bash
# Idempotent one-time setup: raises the Claude Code MCP client-transport timeout
# so a real council call (reasoning tier: deadline_ms=600000, confirmed via direct
# execution against llm-council-core==0.40.1, see docs/upstream-deltas.md) has time
# to complete instead of dying at the client's default 60s cap. Run once per machine
# after cloning this repo, then open a new shell (or `source ~/.bashrc`).
#
# Safe to re-run: skips if the block is already present. Revert: delete the marked
# block from your shell rc file.
set -euo pipefail

MARKER="# >>> llm-council-debate-vote MCP timeout (managed) >>>"
END_MARKER="# <<< llm-council-debate-vote MCP timeout (managed) <<<"
RC_FILE="${LLM_COUNCIL_SHELL_RC:-$HOME/.bashrc}"

if grep -qF "$MARKER" "$RC_FILE" 2>/dev/null; then
    echo "Already configured in $RC_FILE — nothing to do."
    exit 0
fi

cat >>"$RC_FILE" <<EOF

$MARKER
# llm-council calls at the "reasoning"/"high" tier take longer than the MCP
# client's default 60s tool-call timeout (llm-council-core's own per-tier
# deadline_ms is 180000-600000ms — see docs/upstream-deltas.md, "Timeout
# architecture fix"). Only raises the value if nothing stronger is already set.
export MCP_TIMEOUT="\${MCP_TIMEOUT:-900000}"
export MCP_TOOL_TIMEOUT="\${MCP_TOOL_TIMEOUT:-900000}"
$END_MARKER
EOF

echo "Added MCP timeout block to $RC_FILE. Open a new shell (or 'source $RC_FILE') for it to take effect."

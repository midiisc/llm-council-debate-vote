"""One-shot resilient debate CLI. The recommended entry point for an ad hoc
interactive debate instead of the raw mcp__llm-council__consult_council MCP
tool, which is package-native (`run_council_with_fallback` internals) and
cannot be hardened with retry/backup logic without forking the installed
package - see docs/upstream-deltas.md, "Debate resilience" entry, "Scope
decision".

Thin wrapper around scripts/council_adapter.py's run_council_with_timeouts -
the same function pipeline_runner.py's own council_fn already calls - so a
one-shot debate gets identical retry-with-backoff + backup-model
substitution hardening as the full batch pipeline, from the single amended
implementation in council_adapter.py. metadata's optional "shortfall_warning"
(str) and "substitutions" (list of {slot_model, backup_model, reason} dicts)
keys are surfaced here exactly as council_adapter.py's Stage 1 amendment
produces them (docs/specs/debate-resilience-contract.md) - present only
when non-empty, matching the existing "degraded_mode" key convention.

Contract: docs/specs/debate-resilience-contract.md.
"""
from __future__ import annotations

import asyncio
import sys


def _build_arg_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="llm-council-debate",
        description=(
            "Run a one-shot council debate with retry-with-backoff and "
            "backup-model substitution - prefer this over the raw "
            "consult_council MCP tool whenever losing a model to a "
            "transient timeout matters."
        ),
    )
    parser.add_argument("query", help="The question/topic to debate.")
    parser.add_argument("--stage1-timeout", type=float, default=300.0)
    parser.add_argument("--stage2-timeout", type=float, default=300.0)
    parser.add_argument("--stage3-timeout", type=float, default=300.0)
    return parser


def main() -> None:
    from scripts.council_adapter import run_council_with_timeouts

    args = _build_arg_parser().parse_args()

    try:
        _, _, stage3_result, metadata = asyncio.run(
            run_council_with_timeouts(
                args.query,
                stage1_timeout=args.stage1_timeout,
                stage2_timeout=args.stage2_timeout,
                stage3_timeout=args.stage3_timeout,
            )
        )
    except Exception as e:
        print(f"Debate failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(stage3_result.get("response", ""))

    for sub in metadata.get("substitutions") or []:
        print(
            f"NOTE: {sub['slot_model']} was unreachable this session, "
            f"substituted with backup {sub['backup_model']} ({sub['reason']})",
            file=sys.stderr,
        )

    shortfall = metadata.get("shortfall_warning")
    if shortfall:
        print(f"WARNING: {shortfall}", file=sys.stderr)

    if stage3_result.get("model") == "error":
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()

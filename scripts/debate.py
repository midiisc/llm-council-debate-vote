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
from pathlib import Path


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
    # docs/specs/stage1-5-normalizer-timeout-contract.md: replaces the
    # vendored 60s-hardcoded style-normalization timeout (Stage 1 drafts +
    # Stage 2 reviewer commentary) with a configurable budget, same default
    # as the other three `--stageN-timeout` flags.
    parser.add_argument("--stage1-5-timeout", type=float, default=300.0)
    # Parity with pipeline_runner.py's PipelineConfig.max_wall_clock_seconds
    # (docs/architecture-stress-test-2026-08-13.md, High: debate.py had no
    # overall ceiling at all) - same 1200.0 default, always-on backstop.
    parser.add_argument("--max-wall-clock-seconds", type=float, default=1200.0)
    # Parity with pipeline_runner.py's PipelineConfig.max_cost_usd (Medium
    # finding: debate.py had zero cost-ceiling enforcement) - optional,
    # None means no ceiling, matching PipelineConfig's own semantics.
    # Mutation-testing note (2026-08-13): `default=None` is argparse's own
    # implicit default for `add_argument`, so dropping it is a true
    # equivalent mutant (mirrors pipeline_runner.py's `_build_arg_parser`,
    # same reasoning). Verified by direct execution (mutmut run, 1 survivor,
    # traced by hand).
    parser.add_argument("--max-cost-usd", type=float, default=None)
    # Durable persistence (docs/specs/durable-persistence-contract.md) - a
    # one-shot debate previously wrote nothing anywhere, so the answer was
    # only ever visible in a terminal that might get closed. Folder-scoped,
    # timestamped, mirroring pipeline_runner.py's make_output_dir pattern.
    parser.add_argument("--output-root", type=Path, default=Path("./council-runs"))
    return parser


def main() -> None:
    from scripts.council_adapter import run_council_with_timeouts

    args = _build_arg_parser().parse_args()

    try:
        _, _, stage3_result, metadata = asyncio.run(
            asyncio.wait_for(
                run_council_with_timeouts(
                    args.query,
                    stage1_timeout=args.stage1_timeout,
                    stage2_timeout=args.stage2_timeout,
                    stage3_timeout=args.stage3_timeout,
                    stage1_5_timeout=args.stage1_5_timeout,
                    overall_wall_clock_seconds=args.max_wall_clock_seconds,
                ),
                timeout=args.max_wall_clock_seconds,
            )
        )
    except asyncio.TimeoutError:
        print(
            f"Debate failed: exceeded max_wall_clock_seconds "
            f"({args.max_wall_clock_seconds}s)",
            file=sys.stderr,
        )
        sys.exit(4)
    except Exception as e:
        print(f"Debate failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(stage3_result.get("response", ""))

    if stage3_result.get("model") != "error":
        from datetime import datetime, timezone

        from scripts.pipeline_runner import make_output_dir
        from scripts.transcript_writer import write_synthesis

        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
            output_dir = make_output_dir(args.output_root, args.query[:60], timestamp)
            write_synthesis(
                output_dir, stage3_result.get("response", ""), stage3_result.get("model", "unknown")
            )
        except Exception as e:
            print(f"WARNING: failed to persist synthesis.md ({e})", file=sys.stderr)

    for sub in metadata.get("substitutions") or []:
        print(
            f"NOTE: {sub['slot_model']} was unreachable this session, "
            f"substituted with backup {sub['backup_model']} ({sub['reason']})",
            file=sys.stderr,
        )

    shortfall = metadata.get("shortfall_warning")
    if shortfall:
        print(f"WARNING: {shortfall}", file=sys.stderr)

    total_cost_usd = metadata.get("usage", {}).get("total", {}).get("cost_usd", 0.0)
    print(f"Total cost: ${total_cost_usd:.4f}", file=sys.stderr)

    if stage3_result.get("model") == "error":
        sys.exit(1)

    if args.max_cost_usd is not None and total_cost_usd > args.max_cost_usd:
        print(
            f"WARNING: total cost ${total_cost_usd:.4f} exceeded "
            f"max_cost_usd (${args.max_cost_usd:.4f})",
            file=sys.stderr,
        )
        sys.exit(3)

    sys.exit(0)


if __name__ == "__main__":
    main()

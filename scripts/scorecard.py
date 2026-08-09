"""Folder-scoped scorecard log + confidence-gated, non-prescriptive
statistics reporting. Never auto-recommends keep/drop.

Contract: docs/specs/custom-scripts-contracts.md, Contract 3.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal, Optional


@dataclass
class ScorecardRecord:
    timestamp: str
    topic_label: str
    css: float
    rubric_scores: dict[str, dict[str, float]]  # model -> {accuracy, relevance, completeness, conciseness, clarity}
    ranks: dict[str, int]  # model -> Stage 2 Borda rank
    is_outlier: dict[str, bool]  # model -> dissent-flagged this session
    cost_usd: dict[str, float]  # model -> this session's cost share


@dataclass
class ScorecardReport:
    session_count: int
    tier: str
    model_avg_vs_others: dict[str, float] = field(default_factory=dict)  # per rubric dimension, target model avg minus mean-of-others avg
    outlier_sessions: list[tuple[str, str]] = field(default_factory=list)  # (timestamp, topic_label)
    cost_share_pct: float = 0.0


def build_scorecard_record(session_result: dict, topic_label: str, timestamp: str) -> ScorecardRecord:
    return ScorecardRecord(
        timestamp=timestamp,
        topic_label=topic_label,
        css=session_result["css"],
        rubric_scores=session_result["rubric_scores"],
        ranks=session_result["ranks"],
        is_outlier=session_result["is_outlier"],
        cost_usd=session_result["cost_usd"],
    )


def default_scorecard_path(cwd: Path) -> Path:
    # cwd / "council-runs" / "scorecard.jsonl" — never ~/.llm-council/
    return cwd / "council-runs" / "scorecard.jsonl"


def append_record(record: ScorecardRecord, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(record)) + "\n")


def load_records(
    path: Path, cross_folder: bool = False, search_root: Optional[Path] = None
) -> list[ScorecardRecord]:
    # cross_folder=True requires search_root; walks for council-runs/scorecard.jsonl files
    if cross_folder:
        if search_root is None:
            raise ValueError("cross_folder=True requires search_root")
        paths = sorted(search_root.rglob("council-runs/scorecard.jsonl"))
    else:
        paths = [path]

    records: list[ScorecardRecord] = []
    for p in paths:
        if not p.exists():
            continue
        with p.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                records.append(ScorecardRecord(**data))
    return records


def confidence_tier(n: int) -> Literal["insufficient", "preliminary", "moderate", "high"]:
    # n<10 insufficient, 10<=n<20 preliminary, 20<=n<50 moderate, n>=50 high
    if n < 10:
        return "insufficient"
    if n < 20:
        return "preliminary"
    if n < 50:
        return "moderate"
    return "high"


def compute_report(records: list[ScorecardRecord], target_model: str) -> ScorecardReport:
    session_count = len(records)
    tier = confidence_tier(session_count)

    dimensions: set[str] = set()
    for record in records:
        for scores in record.rubric_scores.values():
            dimensions.update(scores.keys())

    model_avg_vs_others: dict[str, float] = {}
    for dim in sorted(dimensions):
        target_scores: list[float] = []
        other_session_avgs: list[float] = []
        for record in records:
            scores = record.rubric_scores
            target_dim_scores = scores.get(target_model, {})
            if dim in target_dim_scores:
                target_scores.append(target_dim_scores[dim])

            others = [
                model_scores[dim]
                for model, model_scores in scores.items()
                if model != target_model and dim in model_scores
            ]
            if others:
                other_session_avgs.append(sum(others) / len(others))

        target_avg = sum(target_scores) / len(target_scores) if target_scores else 0.0
        others_avg = sum(other_session_avgs) / len(other_session_avgs) if other_session_avgs else 0.0
        model_avg_vs_others[dim] = target_avg - others_avg

    outlier_sessions = [
        (record.timestamp, record.topic_label)
        for record in records
        if record.is_outlier.get(target_model, False)
    ]

    total_cost = sum(sum(record.cost_usd.values()) for record in records)
    target_cost = sum(record.cost_usd.get(target_model, 0.0) for record in records)
    cost_share_pct = (target_cost / total_cost * 100.0) if total_cost > 0 else 0.0

    return ScorecardReport(
        session_count=session_count,
        tier=tier,
        model_avg_vs_others=model_avg_vs_others,
        outlier_sessions=outlier_sessions,
        cost_share_pct=cost_share_pct,
    )


def render_report(report: ScorecardReport, target_model: str) -> str:
    # Plain-language, non-prescriptive: counts, tier, averages, outlier list
    # only — never a keep/drop/recommend verdict.
    if report.session_count == 0:
        return "No sessions recorded yet."

    lines = [
        f"Scorecard for {target_model}",
        f"Sessions recorded: {report.session_count} (confidence tier: {report.tier})",
        "",
        "Average rubric score vs. mean of other models:",
    ]
    if report.model_avg_vs_others:
        for dim in sorted(report.model_avg_vs_others):
            diff = report.model_avg_vs_others[dim]
            sign = "+" if diff >= 0 else ""
            lines.append(f"  {dim}: {sign}{diff:.3f}")
    else:
        lines.append("  (no rubric data)")

    lines.append("")
    lines.append(f"Cost share: {report.cost_share_pct:.1f}% of total recorded council spend")
    lines.append("")

    if report.outlier_sessions:
        lines.append("Sessions where this model was flagged as an outlier (for manual review):")
        for timestamp, topic_label in report.outlier_sessions:
            lines.append(f"  - {timestamp} — {topic_label}")
    else:
        lines.append("No outlier-flagged sessions.")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="scorecard", description="Report confidence-gated LLM council scorecard statistics."
    )
    parser.add_argument(
        "--target-model", required=True, help="Model name to report statistics for."
    )
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Path to scorecard.jsonl (default: <cwd>/council-runs/scorecard.jsonl).",
    )
    parser.add_argument(
        "--cross-folder",
        action="store_true",
        help="Aggregate scorecard.jsonl files found under --search-root.",
    )
    parser.add_argument(
        "--search-root",
        type=Path,
        default=None,
        help="Root directory to walk when --cross-folder is set.",
    )
    args = parser.parse_args()

    path = args.path or default_scorecard_path(Path.cwd())
    records = load_records(path, cross_folder=args.cross_folder, search_root=args.search_root)
    report = compute_report(records, args.target_model)
    print(render_report(report, args.target_model))


if __name__ == "__main__":
    main()

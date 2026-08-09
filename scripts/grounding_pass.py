"""Stage 0.5 grounding pass: tag numbered factual claims against
dependency-injected, pre-fetched evidence and render the annotated file that
feeds Stage 1 prompts.

Contract: docs/specs/custom-scripts-contracts.md, Contract 1.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Numbered claim lines, e.g. "3. Some claim text" (possibly wrapping to
# following lines) up to the next numbered line or end of text. Preserves the
# original id string verbatim and never renumbers.
_CLAIM_PATTERN = re.compile(
    r"^[ \t]*(\d+)\.[ \t]+(.*?)(?=^[ \t]*\d+\.[ \t]+|\Z)",
    re.MULTILINE | re.DOTALL,
)


@dataclass
class Evidence:
    source: str
    date: str
    supports: bool  # True = corroborates the claim, False = contradicts it


@dataclass
class Claim:
    id: str  # preserves original numbering, e.g. "3"
    text: str


@dataclass
class TaggedClaim:
    claim: Claim
    tag: Literal["VERIFIED", "CONTRADICTED", "UNVERIFIABLE"]
    evidence: list[Evidence] = field(default_factory=list)


def parse_claims(raw_text: str) -> list[Claim]:
    """Parse numbered claims from raw_text, preserving original id strings."""
    claims: list[Claim] = []
    for match in _CLAIM_PATTERN.finditer(raw_text):
        claim_id = match.group(1)
        text = match.group(2).strip()
        claims.append(Claim(id=claim_id, text=text))
    return claims


def tag_claim(claim: Claim, evidence: list[Evidence]) -> TaggedClaim:
    """Tag a single claim VERIFIED/CONTRADICTED/UNVERIFIABLE.

    Conservative: any contradicting evidence wins over supporting evidence.
    No evidence at all -> UNVERIFIABLE.
    """
    if not evidence:
        tag: Literal["VERIFIED", "CONTRADICTED", "UNVERIFIABLE"] = "UNVERIFIABLE"
    elif any(not e.supports for e in evidence):
        tag = "CONTRADICTED"
    else:
        tag = "VERIFIED"
    return TaggedClaim(claim=claim, tag=tag, evidence=list(evidence))


def render_output(tagged: list[TaggedClaim], original_text: str) -> str:
    """Render the annotated grounding.md content for a list of tagged claims."""
    lines: list[str] = ["# Grounding Pass", ""]
    for tc in tagged:
        lines.append(f"## Claim {tc.claim.id}")
        lines.append(tc.claim.text)
        if tc.tag == "UNVERIFIABLE":
            lines.append("**Tag:** UNVERIFIABLE (demoted to ASSUMPTION)")
        else:
            lines.append(f"**Tag:** {tc.tag}")
        if tc.evidence:
            lines.append("**Evidence:**")
            for ev in tc.evidence:
                verdict = "supports" if ev.supports else "contradicts"
                lines.append(f"- {ev.source} ({ev.date}) — {verdict}")
        else:
            lines.append("**Evidence:** none")
        lines.append("")
    return "\n".join(lines)


def run_grounding_pass(
    input_path: Path,
    evidence: dict[str, list[Evidence]],  # claim.id -> evidence list
    output_dir: Path,
) -> Path:
    """Parse, tag, render, and write grounding.md under output_dir."""
    raw_text = input_path.read_text()
    claims = parse_claims(raw_text)
    tagged = [tag_claim(claim, evidence.get(claim.id, [])) for claim in claims]

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "grounding.md"
    output_path.write_text(render_output(tagged, raw_text))
    return output_path

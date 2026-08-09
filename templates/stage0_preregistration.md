<!--
Stage 0 — Pre-registration (MANUAL, outside the tool)

Fill this in BEFORE any model sees the question. This is not a formality —
per the pipeline's own grounding (Choi/Zhu/Li, NeurIPS 2025 Spotlight,
arXiv:2508.17536) and decision-science practice generally, committing to
criteria before seeing output is what blocks post-hoc rationalization.
If you write this after running the council "just to structure the notes,"
it does nothing - the whole point is that it happens first.

Copy this file to council-runs/<timestamp>-<slug>/stage0.md for the record.
This file's contents are the ONLY source of domain-specific language the
rest of the pipeline (lens questions, premortem categories) is allowed to
use - see templates/premortem_prompt.md and pipeline-architecture-spec.md §6.
-->

# Decision

<!-- One sentence: what is actually being decided. -->

# Options under consideration

<!-- List every option seriously on the table. Include "do nothing" /
     "status quo" explicitly if it's a real option, not just implied. -->

1.
2.
3.

# Weighted criteria

<!-- What actually matters for THIS decision, and how much each matters
     relative to the others. Weights should sum to something sensible
     (e.g. 100%) but exact precision matters less than forcing yourself to
     rank them before you see any answer. -->

| Criterion | Weight | Why it matters here |
|---|---|---|
| | | |
| | | |
| | | |

# Kill-switches

<!-- Conditions that would make you reject an option outright regardless of
     how well it scores on the criteria above. Non-negotiables. -->

-
-

# What would change my mind

<!-- Optional but useful: what evidence, if it showed up during Stage 0.5
     grounding or the council's deliberation, would make you seriously
     reconsider your current leaning (if you have one)? Naming this before
     you see the council's answer makes it easier to notice later if you're
     rationalizing rather than actually updating. -->


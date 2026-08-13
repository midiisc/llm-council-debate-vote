# Stage 5 reasoning-graph contract (Pillar 2 — spec before code)

Status: ready for blind-TDV. Decision grounding:
`docs/citation-and-structured-reasoning-decision-2026-08-13.md`, "Follow-up:
post-hoc structured-artifact stage" (3-judge design panel, unanimous on
unification, user requested — "debate and decide and proceed").

## Problem this closes

The pipeline's synthesis and its supporting evidence exist only as
ephemeral in-memory objects and prose printed to stdout — nothing lets a
human (or a future automated reader) audit *how* the chairman's answer
relates to the facts that grounded it, after the fact. User asked for a
reasoning graph / knowledge graph / concept graph / mind map / reference-
grounding graph "for later use and grounding." The design panel found these
five named things collapse into one schema without loss (KG/CG overlap
heavily in practice; a mind map is a rendering choice; the reference-
grounding graph substantially overlaps Proposal A's own citation mechanism)
— so this contract specs ONE new stage producing ONE typed graph, not five
extraction passes.

## Contract — `scripts/reasoning_graph.py`

**Objective:** given the Stage 3 synthesis and Stage 0.5's verified facts,
produce one versioned JSON graph (reference nodes/edges built
deterministically, concept/claim nodes/edges built from one gated,
span-validated LLM call) plus a human-legible Mermaid rendering — written to
the run's own output directory, self-contained, never feeding back into any
upstream stage's scoring or gating.

**Signature:**
```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

NodeType = Literal["concept", "claim", "reference"]
EdgeType = Literal["relates-to", "supports", "contradicts", "derives-from", "cites"]

@dataclass
class GraphNode:
    id: str
    type: NodeType
    label: str                          # <=80 chars
    source_span: Optional[str]           # required for claim/reference, None for concept
    origin: str                          # "stage3:synthesis" | "verified_facts:<claim.id>"

@dataclass
class GraphEdge:
    id: str
    from_id: str
    to_id: str
    type: EdgeType
    source_span: Optional[str]           # required for supports/contradicts/derives-from, None for relates-to/cites

@dataclass
class ReasoningGraph:
    schema_version: str                  # "1.0"
    run_id: str
    generated_by: str                    # model id used for the LLM extraction call
    generated_at: str                    # ISO8601 UTC
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    dropped_node_count: int
    dropped_edge_count: int

QueryFn = Callable[[str, str, float], Awaitable[Optional[dict]]]
# matches live_adapters.real_query_model's shape: (model, prompt, timeout) -> {"content": str, "usage": dict, "cost_usd": float} | None

def build_reference_nodes_and_edges(
    verified_facts: list["TaggedClaim"],
    dropped_fact_ids: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Pure, deterministic, zero LLM calls. One reference node per
    TaggedClaim (id=f"ref:{tc.claim.id}"), one cites edge per node UNLESS
    tc.claim.id is in dropped_fact_ids (Stage 4's dropped_facts) -- an
    unaddressed fact renders as a disconnected reference node, free
    synergy with Stage 4's already-computed output."""
    ...

def build_extraction_prompt(synthesis_text: str, reference_node_ids: list[str]) -> str:
    """One prompt, synthesis text only (never the 4 Stage-1 drafts --
    keeps prompt cost and injection surface down per the design panel's
    explicit narrowing). Instructs the model to emit concept/claim nodes
    and relates-to/supports/contradicts/derives-from edges as JSON, each
    claim node and each supports/contradicts/derives-from edge carrying a
    verbatim source_span. May reference existing reference_node_ids in
    `cites`-typed edges FROM a claim node TO a reference node -- the only
    edge type allowed to cross into the deterministic half of the graph."""
    ...

def parse_and_validate_extraction(
    response_text: str,
    synthesis_text: str,
    known_node_ids: set[str],  # includes the deterministic reference node ids
) -> tuple[list[GraphNode], list[GraphEdge], int, int]:
    """Never raises -- malformed JSON degrades to (existing nodes/edges
    unchanged, 0, 0) plus a caller-visible signal via the returned counts
    being unreliable (caller must also check for a parse-failure flag --
    see AC7). Validates: (a) every claim node's source_span is a literal
    substring of synthesis_text; (b) every supports/contradicts/
    derives-from edge's source_span is a literal substring of
    synthesis_text; (c) every edge's from/to resolve to a known node id
    (existing reference nodes OR a node emitted in this same response).
    Anything failing (a)/(b)/(c) is DROPPED and counted -- never kept with
    a flag. Returns (kept_nodes, kept_edges, dropped_node_count, dropped_edge_count)."""
    ...

async def build_reasoning_graph(
    run_id: str,
    synthesis_text: str,
    verified_facts: list["TaggedClaim"],
    dropped_fact_ids: set[str],
    model: str,
    query_fn: QueryFn,
    timeout: float,
) -> tuple[Optional[ReasoningGraph], Optional[str]]:
    """Returns (graph, skip_reason). skip_reason is None iff graph is not
    None. Never raises -- any exception during the LLM call or parsing is
    caught and returned as skip_reason="extraction_error" (AC8). Does NOT
    perform cost-ceiling or wall-clock-budget gating itself -- that is the
    caller's (pipeline_runner.py's) responsibility, matching every other
    conditional stage's existing division of labor in this codebase."""
    ...

def render_markdown(graph: ReasoningGraph) -> str:
    """Mermaid `graph TD` -- concept=rounded ([( )]), claim=rectangle ([ ]),
    reference=stadium ([( )] variant / ([ ])). Edges labeled by type. Always
    ends with the fixed disclaimer line (AC10)."""
    ...

def write_reasoning_graph_files(
    output_dir: Path, graph: ReasoningGraph, synthesis_text: str,
) -> tuple[Path, Path, Path]:
    """Writes reasoning_graph.json, reasoning_graph.md, synthesis.md into
    output_dir. Returns the three paths written. Never raises for a
    pre-existing directory (output_dir already exists by the time this is
    called, per pipeline_runner.py's existing make_output_dir contract)."""
    ...
```

**Acceptance criteria (Given/When/Then):**

1. Given `verified_facts` is a list of N `TaggedClaim`s and
   `dropped_fact_ids` is empty, When `build_reference_nodes_and_edges` runs,
   Then it returns exactly N `GraphNode`s (type=`reference`, id=`f"ref:{tc.claim.id}"`,
   `source_span == tc.claim.text`, `origin == f"verified_facts:{tc.claim.id}"`)
   and exactly N `GraphEdge`s (type=`cites`) — one per node — with no LLM
   call involved (verifiable: this function takes no `query_fn`/model
   argument at all).

2. Given a `TaggedClaim.id` is present in `dropped_fact_ids`, When
   `build_reference_nodes_and_edges` runs, Then that claim's reference node
   is still created, but no `cites` edge is created for it — a
   disconnected node, not an absent one.

3. Given an LLM extraction response containing a claim node whose
   `source_span` is a literal substring of `synthesis_text`, When
   `parse_and_validate_extraction` runs, Then that node is kept.

4. Given a claim node whose `source_span` is NOT a literal substring of
   `synthesis_text` (fabricated/paraphrased), When it runs, Then that node
   is dropped and `dropped_node_count` increments by 1 — never kept with a
   flag, never silently ignored without incrementing the count.

5. Given a `concept`-type node with no `source_span` at all, When it runs,
   Then it is kept without a span check — concept nodes are exempt from
   span-anchoring (legitimate cross-span abstraction, per the design
   panel's explicit distinction from first-order factual nodes).

6. Given a `supports`/`contradicts`/`derives-from` edge whose `source_span`
   is not a literal substring of `synthesis_text`, OR whose `from`/`to`
   does not resolve to a known node id, When it runs, Then that edge is
   dropped and `dropped_edge_count` increments — same drop-and-count rule
   as node validation, no exceptions for edges.

7. Given the LLM response is malformed (not valid JSON, or valid JSON
   missing the expected `nodes`/`edges` keys), When
   `parse_and_validate_extraction` runs, Then it returns an empty
   nodes/edges list and the caller (`build_reasoning_graph`) surfaces this
   distinctly (via `skip_reason`, not by silently returning a
   graph containing only the deterministic reference half with no
   indication the LLM half failed) — matches this project's existing
   `completeness_check_parse_failed` "distinguish couldn't-run from
   ran-and-clean" precedent.

8. Given `query_fn` raises (network error) or returns `None`, When
   `build_reasoning_graph` runs, Then it returns `(None, "extraction_error")`
   — the deterministic reference nodes/edges are NOT persisted on their own
   in this case (an all-or-nothing graph per run, not a partial one that
   could be mistaken for complete); no exception propagates to the caller.

9. Given a successful extraction, When `build_reasoning_graph` returns,
   Then the returned `ReasoningGraph.nodes` is the deterministic reference
   set UNION the validated concept/claim set, and `.edges` is the
   deterministic cites set UNION the validated relationship set — a single
   merged graph, not two separate outputs the caller must combine itself.

10. Given a `ReasoningGraph`, When `render_markdown` runs, Then the output
    is a fenced ` ```mermaid ` block using `graph TD` syntax, every node
    rendered with a shape keyed to its `type` (concept/claim/reference each
    visually distinct), every edge labeled with its `type`, and the output
    ends with the fixed disclaimer sentence: "Machine-extracted from the
    synthesis above; edges are span-validated against source text but a
    fabricated relationship between two genuinely-present spans is not
    detectable by this check — treat as an audit aid, not a proof." —
    verbatim, every time, regardless of graph content.

11. Given `output_dir` already exists (created by `pipeline_runner.py`'s
    `make_output_dir` before this stage ever runs), When
    `write_reasoning_graph_files` runs, Then it writes exactly 3 files
    (`reasoning_graph.json`, `reasoning_graph.md`, `synthesis.md`) into
    that directory and returns their paths — `synthesis.md` contains
    `synthesis_text` verbatim, no additional wrapping.

12. Given the full `ReasoningGraph` dataclass, When serialized to
    `reasoning_graph.json`, Then the JSON is round-trippable (parseable
    back into an equivalent structure) and includes `schema_version`,
    `run_id`, `generated_by`, `generated_at`, `dropped_node_count`,
    `dropped_edge_count` at the top level, alongside `nodes`/`edges`.

**Non-goals:** no cost-ceiling or wall-clock-budget gating inside this
module — `pipeline_runner.py` owns that (mirroring every other conditional
stage's split: the stage module does the work, the runner decides whether
to call it). No consumption of the 4 raw Stage 1 responses (deliberately
narrowed by the design panel — synthesis-only keeps prompt cost and
injection surface down). No wiring of `safety_check` results into this
module (a repo-wide gap shared by Stage 2.75/Stage 4, explicitly out of
scope for this new-stage spec — flagged separately, not silently bundled
in). No dependency on or fix for the general lack of durable persistence
elsewhere in the pipeline (Critical #7 in
`docs/architecture-stress-test-2026-08-13.md`) — this stage's 3 files are
self-contained.

## Integration (`pipeline_runner.py`)

Hook point: `_run_stages()`, immediately after the existing Stage 4 block
ends and before `rubric_scores = extract_rubric_scores_for_scorecard(...)`.
Gating, all three must pass (else skip with an explicit
`reasoning_graph_skipped_reason`, never a silent absence):
1. `cost_so_far >= config.max_cost_usd` (if set) → skip, reason
   `"cost_ceiling"` — same idiom as Stage 4's existing check.
2. New wall-clock soft-budget self-check: capture `stage_start =
   time.monotonic()` at the top of `_run_stages()`; skip if
   `time.monotonic() - stage_start > config.max_wall_clock_seconds - 60.0`,
   reason `"wall_clock_margin"`. Stated limitation, not fixed by this
   contract: this only helps if `_run_stages()` reaches this check at all —
   the outer `asyncio.wait_for` can still fire first per
   `docs/architecture-stress-test-2026-08-13.md`'s Critical #3, discarding
   this stage along with everything else; that is a pre-existing gap, out
   of this contract's scope.
3. `try/except Exception` around the whole extraction+write block, same
   best-effort idiom `audition_tracking`'s call site already uses — any
   exception sets `reasoning_graph_skipped_reason = "extraction_error"` and
   never fails an otherwise-successful run.

New `PipelineResult` fields: `reasoning_graph_path: Optional[Path] = None`,
`reasoning_graph_skipped_reason: Optional[str] = None`,
`reasoning_graph_dropped_count: Optional[dict] = None`. New `debug_log`
line: `"Stage 5: graph extracted, N node(s) kept (D dropped), M edge(s)
kept (E dropped)"` or the explicit skip reason.

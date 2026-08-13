"""Stage 5: post-synthesis reasoning graph. Given the Stage 3 synthesis and
Stage 0.5's verified facts, produces one versioned JSON graph (reference
nodes/edges built deterministically, concept/claim nodes/edges built from one
gated, span-validated LLM call) plus a human-legible Mermaid rendering,
written to the run's own output directory. Self-contained -- never feeds
back into any upstream stage's scoring or gating.

Contract: docs/specs/reasoning-graph-contract.md.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Literal, Optional

from scripts.grounding_pass import TaggedClaim

NodeType = Literal["concept", "claim", "reference"]
EdgeType = Literal["relates-to", "supports", "contradicts", "derives-from", "cites"]

SCHEMA_VERSION = "1.0"

# Edge types whose source_span must be a literal substring of the synthesis
# text (contract's validation rule (b)). relates-to/cites are exempt, per
# the dataclass docstring ("None for relates-to/cites").
_SPAN_REQUIRED_EDGE_TYPES = frozenset({"supports", "contradicts", "derives-from"})

# The extraction LLM is only ever instructed to emit concept/claim nodes
# (reference nodes are deterministic-only, built by
# build_reference_nodes_and_edges) - any other type in a parsed response is
# treated as malformed and dropped.
_VALID_LLM_NODE_TYPES = frozenset({"concept", "claim"})
_VALID_EDGE_TYPES = frozenset({"relates-to", "supports", "contradicts", "derives-from", "cites"})

DISCLAIMER = (
    "Machine-extracted from the synthesis above; edges are span-validated "
    "against source text but a fabricated relationship between two "
    "genuinely-present spans is not detectable by this check — treat as an "
    "audit aid, not a proof."
)

_MERMAID_NODE_SHAPE: dict[str, tuple[str, str]] = {
    "concept": ("(", ")"),  # rounded rectangle
    "claim": ("[", "]"),  # rectangle
    "reference": ("([", "])"),  # stadium
}


@dataclass
class GraphNode:
    id: str
    type: NodeType
    label: str  # <=80 chars
    source_span: Optional[str]  # required for claim/reference, None for concept
    origin: str  # "stage3:synthesis" | "verified_facts:<claim.id>"


@dataclass
class GraphEdge:
    id: str
    from_id: str
    to_id: str
    type: EdgeType
    source_span: Optional[str]  # required for supports/contradicts/derives-from, None for relates-to/cites


@dataclass
class ReasoningGraph:
    schema_version: str
    run_id: str
    generated_by: str  # model id used for the LLM extraction call
    generated_at: str  # ISO8601 UTC
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    dropped_node_count: int
    dropped_edge_count: int


# matches live_adapters.real_query_model's shape: (model, prompt, timeout) ->
# {"content": str, "usage": dict, "cost_usd": float} | None
QueryFn = Callable[[str, str, float], Awaitable[Optional[dict]]]


def build_reference_nodes_and_edges(
    verified_facts: list[TaggedClaim],
    dropped_fact_ids: set[str],
) -> tuple[list[GraphNode], list[GraphEdge]]:
    """Pure, deterministic, zero LLM calls. One reference node per
    TaggedClaim (id=f"ref:{tc.claim.id}"), one cites edge per node UNLESS
    tc.claim.id is in dropped_fact_ids (Stage 4's dropped_facts) -- an
    unaddressed fact renders as a disconnected reference node, free
    synergy with Stage 4's already-computed output."""
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for tc in verified_facts:
        node_id = f"ref:{tc.claim.id}"
        nodes.append(
            GraphNode(
                id=node_id,
                type="reference",
                label=tc.claim.text[:80],
                source_span=tc.claim.text,
                origin=f"verified_facts:{tc.claim.id}",
            )
        )
        if tc.claim.id not in dropped_fact_ids:
            edges.append(
                GraphEdge(
                    id=f"cites:{tc.claim.id}",
                    from_id=node_id,
                    to_id=node_id,
                    type="cites",
                    source_span=None,
                )
            )
    return nodes, edges


def build_extraction_prompt(synthesis_text: str, reference_node_ids: list[str]) -> str:
    """One prompt, synthesis text only (never the 4 Stage-1 drafts --
    keeps prompt cost and injection surface down per the design panel's
    explicit narrowing). Instructs the model to emit concept/claim nodes
    and relates-to/supports/contradicts/derives-from edges as JSON, each
    claim node and each supports/contradicts/derives-from edge carrying a
    verbatim source_span. May reference existing reference_node_ids in
    `cites`-typed edges FROM a claim node TO a reference node -- the only
    edge type allowed to cross into the deterministic half of the graph."""
    ref_ids_block = ", ".join(reference_node_ids) if reference_node_ids else "(none)"
    return (
        "Below is a final synthesized answer. Extract a small reasoning "
        "graph from it: concept nodes for abstractions/themes, and claim "
        "nodes for first-order factual assertions.\n\n"
        "Rules:\n"
        "- Every claim node's source_span MUST be a verbatim, literal "
        "substring copied exactly from the synthesis text below - never "
        "paraphrased.\n"
        "- Concept nodes do not need a source_span (use null).\n"
        "- Edges may be of type relates-to, supports, contradicts, "
        "derives-from, or cites.\n"
        "- Every supports/contradicts/derives-from edge's source_span MUST "
        "also be a verbatim, literal substring of the synthesis text.\n"
        "- cites edges may point FROM a claim node you emit TO one of these "
        "existing reference node ids (do not invent new reference ids): "
        f"{ref_ids_block}.\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no other "
        "text) in exactly this shape:\n"
        '{"nodes": [{"id": "...", "type": "concept"|"claim", '
        '"label": "...", "source_span": "..."|null}], '
        '"edges": [{"id": "...", "from_id": "...", "to_id": "...", '
        '"type": "relates-to"|"supports"|"contradicts"|"derives-from"|"cites", '
        '"source_span": "..."|null}]}\n\n'
        "Synthesis text:\n"
        f"{synthesis_text}"
    )


def _try_parse_extraction_json(response_text: str) -> Optional[dict]:
    """Never raises. Returns the parsed dict iff response_text is valid
    JSON, an object, and has both `nodes` and `edges` keys as lists --
    otherwise None (the "malformed" signal AC7 requires callers to check
    for separately from an empty-but-valid extraction)."""
    try:
        data = json.loads(response_text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    nodes = data.get("nodes")
    edges = data.get("edges")
    # Mutation-testing note (2026-08-13): flipping this `or` to `and` survives
    # mutmut but is a documented equivalent mutant, not a real gap - when
    # exactly one key is absent, .get() returns None for it and the
    # isinstance(..., list) check below independently rejects None, so the
    # two branches are behaviorally identical for every input. Verified by
    # direct execution (mutmut run, 1 survivor, traced by hand).
    if "nodes" not in data or "edges" not in data:
        return None
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None
    return data


def _coerce_node(raw: object) -> Optional[GraphNode]:
    if not isinstance(raw, dict):
        return None
    node_id = raw.get("id")
    node_type = raw.get("type")
    if not isinstance(node_id, str) or not node_id:
        return None
    if node_type not in _VALID_LLM_NODE_TYPES:
        return None
    label = raw.get("label")
    if not isinstance(label, str):
        label = "" if label is None else str(label)
    source_span = raw.get("source_span")
    if source_span is not None and not isinstance(source_span, str):
        source_span = None
    return GraphNode(
        id=node_id,
        type=node_type,
        label=label[:80],
        source_span=source_span,
        origin="stage3:synthesis",
    )


def _coerce_edge(raw: object) -> Optional[GraphEdge]:
    if not isinstance(raw, dict):
        return None
    edge_id = raw.get("id")
    from_id = raw.get("from_id")
    to_id = raw.get("to_id")
    edge_type = raw.get("type")
    if not isinstance(edge_id, str) or not edge_id:
        return None
    if not isinstance(from_id, str) or not isinstance(to_id, str):
        return None
    if edge_type not in _VALID_EDGE_TYPES:
        return None
    source_span = raw.get("source_span")
    if source_span is not None and not isinstance(source_span, str):
        source_span = None
    return GraphEdge(
        id=edge_id,
        from_id=from_id,
        to_id=to_id,
        type=edge_type,
        source_span=source_span,
    )


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
    data = _try_parse_extraction_json(response_text)
    if data is None:
        return [], [], 0, 0

    available_ids: set[str] = set(known_node_ids)
    kept_nodes: list[GraphNode] = []
    dropped_node_count = 0

    for raw in data.get("nodes") or []:
        node = _coerce_node(raw)
        if node is None:
            dropped_node_count += 1
            continue
        if node.type == "concept":
            kept_nodes.append(node)
            available_ids.add(node.id)
            continue
        # claim node: source_span must be a literal substring of synthesis_text
        if node.source_span is not None and node.source_span in synthesis_text:
            kept_nodes.append(node)
            available_ids.add(node.id)
        else:
            dropped_node_count += 1

    kept_edges: list[GraphEdge] = []
    dropped_edge_count = 0

    for raw in data.get("edges") or []:
        edge = _coerce_edge(raw)
        if edge is None:
            dropped_edge_count += 1
            continue
        if edge.from_id not in available_ids or edge.to_id not in available_ids:
            dropped_edge_count += 1
            continue
        if edge.type in _SPAN_REQUIRED_EDGE_TYPES:
            if edge.source_span is None or edge.source_span not in synthesis_text:
                dropped_edge_count += 1
                continue
        kept_edges.append(edge)

    return kept_nodes, kept_edges, dropped_node_count, dropped_edge_count


async def build_reasoning_graph(
    run_id: str,
    synthesis_text: str,
    verified_facts: list[TaggedClaim],
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
    reference_nodes, reference_edges = build_reference_nodes_and_edges(
        verified_facts, dropped_fact_ids
    )
    reference_node_ids = [n.id for n in reference_nodes]
    prompt = build_extraction_prompt(synthesis_text, reference_node_ids)

    try:
        response = await query_fn(model, prompt, timeout)
    except Exception:
        return None, "extraction_error"

    if not isinstance(response, dict):
        return None, "extraction_error"

    content = response.get("content")
    if not isinstance(content, str):
        return None, "extraction_error"

    parsed = _try_parse_extraction_json(content)
    if parsed is None:
        return None, "malformed_extraction_response"

    known_ids = set(reference_node_ids)
    llm_nodes, llm_edges, dropped_node_count, dropped_edge_count = parse_and_validate_extraction(
        content, synthesis_text, known_ids
    )

    graph = ReasoningGraph(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        generated_by=model,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        nodes=reference_nodes + llm_nodes,
        edges=reference_edges + llm_edges,
        dropped_node_count=dropped_node_count,
        dropped_edge_count=dropped_edge_count,
    )
    return graph, None


def _sanitize_mermaid_id(node_id: str) -> str:
    """Mermaid node/edge ids may not contain arbitrary punctuation - map
    anything outside [A-Za-z0-9_] to '_' for the diagram only (does not
    touch the underlying GraphNode/GraphEdge id, only the rendered label)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", node_id)


def render_markdown(graph: ReasoningGraph) -> str:
    """Mermaid `graph TD` -- concept=rounded ([( )]), claim=rectangle ([ ]),
    reference=stadium ([( )] variant / ([ ])). Edges labeled by type. Always
    ends with the fixed disclaimer line (AC10)."""
    lines: list[str] = ["```mermaid", "graph TD"]
    for node in graph.nodes:
        open_bracket, close_bracket = _MERMAID_NODE_SHAPE.get(node.type, ("[", "]"))
        label = node.label.replace('"', "'")
        node_ref = _sanitize_mermaid_id(node.id)
        lines.append(f'    {node_ref}{open_bracket}"{label}"{close_bracket}')
    for edge in graph.edges:
        from_ref = _sanitize_mermaid_id(edge.from_id)
        to_ref = _sanitize_mermaid_id(edge.to_id)
        lines.append(f"    {from_ref} -->|{edge.type}| {to_ref}")
    lines.append("```")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def write_reasoning_graph_files(
    output_dir: Path, graph: ReasoningGraph, synthesis_text: str,
) -> tuple[Path, Path, Path]:
    """Writes reasoning_graph.json, reasoning_graph.md, synthesis.md into
    output_dir. Returns the three paths written. Never raises for a
    pre-existing directory (output_dir already exists by the time this is
    called, per pipeline_runner.py's existing make_output_dir contract)."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    json_path = output_dir / "reasoning_graph.json"
    md_path = output_dir / "reasoning_graph.md"
    synthesis_path = output_dir / "synthesis.md"

    json_path.write_text(json.dumps(asdict(graph), indent=2))
    md_path.write_text(render_markdown(graph))
    synthesis_path.write_text(synthesis_text)

    return json_path, md_path, synthesis_path

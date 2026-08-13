"""Blind acceptance tests for the Stage 5 reasoning-graph contract --
`scripts/reasoning_graph.py`.

Source of truth: docs/specs/reasoning-graph-contract.md, Acceptance Criteria
1-12 (plus the signature block's dataclasses/docstrings, which are part of
the contract). Authored WITHOUT sight of any implementation of
reasoning_graph.py, its design reasoning, or any other agent's test
authorship for this contract -- purely from the contract text.

Scope: this file targets ONLY the functions exported by
scripts/reasoning_graph.py (the "Contract -- scripts/reasoning_graph.py"
section). The contract document's "Integration (pipeline_runner.py)"
section describes a *different* artifact (scripts/pipeline_runner.py, which
has its own contract at docs/specs/pipeline-runner-contract.md and its own
test file) -- confirmed pipeline_runner.py currently has no
`reasoning_graph` wiring at all, so that integration is out of scope here.

DOCUMENTED ASSUMPTIONS (the contract underspecifies these; flagged per this
repo's own precedent in tests/test_revision_round.py for an underspecified
wire format):

1. Wire JSON schema for the raw LLM extraction response consumed by
   `parse_and_validate_extraction`. The contract pins the TOP-LEVEL keys
   ("nodes"/"edges" -- AC7 explicitly tests their absence) and the edge
   validation rule references "from/to". Nothing else about per-node/
   per-edge key names is pinned. This suite assumes each node/edge object
   uses the exact GraphNode/GraphEdge dataclass field names as its JSON
   keys (`id`, `type`, `label`, `source_span` for nodes; `id`, `from_id`,
   `to_id`, `type`, `source_span` for edges) -- chosen because AC12
   confirms the *persisted* graph JSON uses the dataclass field names
   verbatim, so assuming the same convention end-to-end is the most
   internally-consistent reading. `origin` is NOT expected from the LLM
   (the signature comments it as a fixed literal, "stage3:synthesis",
   assigned by the validator itself for extraction-derived nodes).
2. `parse_and_validate_extraction`'s own dropped_node_count/
   dropped_edge_count values for the malformed-JSON branch are NOT
   pinned exactly -- its docstring calls those counts "unreliable" for
   that branch. Tests assert only what AC7 clearly requires: empty kept
   nodes/edges lists, and that the function never raises.
3. `build_reasoning_graph`'s exact skip_reason STRING for the
   malformed-response branch (AC7, distinct from AC8) is not pinned by
   the contract -- only AC8's literal string "extraction_error" is
   contract-pinned. Tests assert the invariant clearly stated in AC7/the
   docstring (graph is None, skip_reason is a non-empty string -- i.e.
   never a silent partial graph) without pinning the exact string.
4. `render_markdown`'s node-shape bracket syntax is described
   inconsistently in the docstring ("reference=stadium ([( )] variant /
   ([ ]))"). Tests assert the clear requirement -- concept/claim/
   reference each render with a visually DISTINCT shape -- without
   pinning literal bracket characters.
5. `write_reasoning_graph_files`'s return-tuple ORDER is not re-pinned
   beyond its docstring's write order; tests verify the returned paths by
   filename membership (order-independent) rather than positional
   assumption.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
from pathlib import Path

from hypothesis import given, settings, strategies as st

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _import(name: str):
    try:
        return importlib.import_module(f"scripts.{name}")
    except ModuleNotFoundError:
        return importlib.import_module(name)


gp = _import("grounding_pass")
rg = _import("reasoning_graph")

Claim = gp.Claim
Evidence = gp.Evidence
TaggedClaim = gp.TaggedClaim

GraphNode = rg.GraphNode
GraphEdge = rg.GraphEdge
ReasoningGraph = rg.ReasoningGraph
build_reference_nodes_and_edges = rg.build_reference_nodes_and_edges
build_extraction_prompt = rg.build_extraction_prompt
parse_and_validate_extraction = rg.parse_and_validate_extraction
build_reasoning_graph = rg.build_reasoning_graph
render_markdown = rg.render_markdown
write_reasoning_graph_files = rg.write_reasoning_graph_files

# Private helpers, imported directly ONLY in the "Mutation-gate hardening"
# section below -- every test above this point stays scoped to the public
# contract surface per this file's own blind-authorship scope note.
_coerce_node = rg._coerce_node
_coerce_edge = rg._coerce_edge
_try_parse_extraction_json = rg._try_parse_extraction_json
_sanitize_mermaid_id = rg._sanitize_mermaid_id


DISCLAIMER = (
    "Machine-extracted from the synthesis above; edges are span-validated "
    "against source text but a fabricated relationship between two "
    "genuinely-present spans is not detectable by this check — treat "
    "as an audit aid, not a proof."
)

# A codepoint deliberately excluded from every generated "safe" string below,
# so it can be appended to deliberately corrupt/guarantee-non-substring a
# span in property tests.
_SENTINEL = ""

_safe_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters=_SENTINEL),
    max_size=20,
)
_safe_span_text = st.text(
    alphabet=st.characters(
        blacklist_categories=("Cs", "Cc"), blacklist_characters=_SENTINEL + '"\\'
    ),
    min_size=1,
    max_size=15,
)


def _fact(fid: str, text: str, tag: str = "VERIFIED") -> TaggedClaim:
    return TaggedClaim(
        claim=Claim(id=fid, text=text),
        tag=tag,
        evidence=[Evidence(source="src", date="2024-01-01", supports=(tag != "CONTRADICTED"))],
    )


def _graph(nodes=None, edges=None, **overrides) -> ReasoningGraph:
    fields = dict(
        schema_version="1.0",
        run_id="run-1",
        generated_by="test-model",
        generated_at="2026-08-13T00:00:00Z",
        nodes=nodes or [],
        edges=edges or [],
        dropped_node_count=0,
        dropped_edge_count=0,
    )
    fields.update(overrides)
    return ReasoningGraph(**fields)


def _query_fn_returning(content: str, cost: float = 0.0):
    async def fn(model, prompt, timeout):
        return {"content": content, "usage": {"total_tokens": 10}, "cost_usd": cost}

    return fn


def _query_fn_raising(exc: Exception):
    async def fn(model, prompt, timeout):
        raise exc

    return fn


def _query_fn_returning_none():
    async def fn(model, prompt, timeout):
        return None

    return fn


async def _noop_query_fn(model, prompt, timeout):  # pragma: no cover - safety net only
    raise AssertionError("query_fn should never be called for a pure/deterministic path")


# ---------------------------------------------------------------------------
# AC1: Given verified_facts is a list of N TaggedClaims and dropped_fact_ids
# is empty, When build_reference_nodes_and_edges runs, Then it returns
# exactly N GraphNodes (type=reference, id=f"ref:{tc.claim.id}",
# source_span==tc.claim.text, origin==f"verified_facts:{tc.claim.id}") and
# exactly N GraphEdges (type=cites) -- one per node -- with no LLM call
# involved (verifiable: this function takes no query_fn/model argument).
# ---------------------------------------------------------------------------


def test_ac1_no_llm_argument_in_signature():
    import inspect

    params = inspect.signature(build_reference_nodes_and_edges).parameters
    names = {p.lower() for p in params}
    assert not any("query" in n or "model" in n for n in names)


def test_ac1_pure_reference_nodes_and_cites_edges_no_drops():
    facts = [_fact("1", "Paris is the capital of France"), _fact("2", "Water boils at 100C")]
    nodes, edges = build_reference_nodes_and_edges(facts, dropped_fact_ids=set())

    assert len(nodes) == 2
    assert len(edges) == 2
    for tc, node in zip(facts, nodes):
        assert node.type == "reference"
        assert node.id == f"ref:{tc.claim.id}"
        assert node.source_span == tc.claim.text
        assert node.origin == f"verified_facts:{tc.claim.id}"
        assert node.label == tc.claim.text[:80]  # docstring: label is claim text, <=80 chars
    edge_endpoints = {e.to_id for e in edges} | {e.from_id for e in edges}
    for node in nodes:
        assert node.id in edge_endpoints
    for tc, edge in zip(facts, edges):
        assert edge.type == "cites"
        assert edge.id == f"cites:{tc.claim.id}"
        assert edge.from_id == f"ref:{tc.claim.id}"
        assert edge.to_id == f"ref:{tc.claim.id}"


def test_ac1_reference_node_label_truncated_at_exactly_80_chars():
    # Mutation-gate hardening (2026-08-13): no prior fixture used text
    # longer than 80 chars, so the "[:80]" truncation slice's exact bound
    # (vs. off-by-one variants like "[:81]") was unverified.
    long_text = "X" * 100
    facts = [_fact("1", long_text)]
    nodes, _ = build_reference_nodes_and_edges(facts, dropped_fact_ids=set())
    assert nodes[0].label == "X" * 80
    assert len(nodes[0].label) == 80
    assert nodes[0].source_span == long_text  # source_span is NOT truncated


@settings(max_examples=50, derandomize=True, deadline=500)
@given(ids=st.lists(st.from_regex(r"[1-9][0-9]?", fullmatch=True), min_size=0, max_size=15, unique=True))
def test_ac1_property_node_count_and_ids_always_match_claim_count(ids):
    facts = [_fact(fid, f"claim text {fid}") for fid in ids]
    nodes, edges = build_reference_nodes_and_edges(facts, dropped_fact_ids=set())
    assert len(nodes) == len(facts)
    assert {n.id for n in nodes} == {f"ref:{fid}" for fid in ids}
    assert all(n.type == "reference" for n in nodes)


@settings(max_examples=30, derandomize=True, deadline=500)
@given(
    ids=st.lists(st.from_regex(r"[1-9][0-9]?", fullmatch=True), min_size=0, max_size=10, unique=True),
    data=st.data(),
)
def test_build_reference_nodes_and_edges_property_deterministic_pure_function(ids, data):
    # Docstring: "Pure, deterministic, zero LLM calls" -- calling twice with
    # the same input must yield structurally equal output.
    dropped = data.draw(st.sets(st.sampled_from(ids), max_size=len(ids))) if ids else set()
    facts = [_fact(fid, f"text {fid}") for fid in ids]
    result1 = build_reference_nodes_and_edges(facts, dropped_fact_ids=dropped)
    result2 = build_reference_nodes_and_edges(facts, dropped_fact_ids=dropped)
    assert result1 == result2


# ---------------------------------------------------------------------------
# AC2: Given a TaggedClaim.id is present in dropped_fact_ids, When
# build_reference_nodes_and_edges runs, Then that claim's reference node is
# still created, but no cites edge is created for it -- a disconnected node,
# not an absent one.
# ---------------------------------------------------------------------------


def test_ac2_dropped_fact_still_gets_node_but_no_cites_edge():
    facts = [_fact("1", "kept claim"), _fact("2", "dropped claim")]
    nodes, edges = build_reference_nodes_and_edges(facts, dropped_fact_ids={"2"})

    assert len(nodes) == 2  # disconnected node, not an absent one
    assert {n.id for n in nodes} == {"ref:1", "ref:2"}
    edge_endpoints = {e.to_id for e in edges} | {e.from_id for e in edges}
    assert "ref:1" in edge_endpoints
    assert "ref:2" not in edge_endpoints
    assert len(edges) == 1


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    ids=st.lists(st.from_regex(r"[1-9][0-9]?", fullmatch=True), min_size=1, max_size=15, unique=True),
    data=st.data(),
)
def test_ac2_property_node_count_unaffected_but_edge_count_excludes_dropped(ids, data):
    dropped = data.draw(st.sets(st.sampled_from(ids), max_size=len(ids)))
    facts = [_fact(fid, f"text {fid}") for fid in ids]
    nodes, edges = build_reference_nodes_and_edges(facts, dropped_fact_ids=dropped)

    assert len(nodes) == len(facts)  # every claim always gets a node
    assert len(edges) == len(ids) - len(dropped)
    edge_endpoints = {e.to_id for e in edges} | {e.from_id for e in edges}
    for fid in ids:
        if fid in dropped:
            assert f"ref:{fid}" not in edge_endpoints
        else:
            assert f"ref:{fid}" in edge_endpoints


# ---------------------------------------------------------------------------
# AC3: Given an LLM extraction response containing a claim node whose
# source_span is a literal substring of synthesis_text, When
# parse_and_validate_extraction runs, Then that node is kept.
#
# AC4: Given a claim node whose source_span is NOT a literal substring of
# synthesis_text, When it runs, Then that node is dropped and
# dropped_node_count increments by 1.
#
# Encoded together below as one general law (kept iff literal substring),
# plus one deterministic example test each.
# ---------------------------------------------------------------------------


def test_ac3_claim_node_with_span_that_is_a_literal_substring_is_kept():
    synthesis_text = "The Eiffel Tower is 330 meters tall and was completed in 1889."
    response = {
        "nodes": [{"id": "claim:1", "type": "claim", "label": "Height claim", "source_span": "330 meters tall"}],
        "edges": [],
    }
    kept_nodes, _, dropped_nodes, _ = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert len(kept_nodes) == 1
    assert kept_nodes[0].id == "claim:1"
    assert kept_nodes[0].type == "claim"
    assert kept_nodes[0].source_span == "330 meters tall"
    assert kept_nodes[0].origin == "stage3:synthesis"
    assert dropped_nodes == 0


def test_ac4_claim_node_with_fabricated_span_is_dropped_and_counted():
    synthesis_text = "The Eiffel Tower is 330 meters tall and was completed in 1889."
    response = {
        "nodes": [{"id": "claim:1", "type": "claim", "label": "Height claim", "source_span": "450 meters tall"}],
        "edges": [],
    }
    kept_nodes, _, dropped_nodes, _ = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert kept_nodes == []
    assert dropped_nodes == 1


@settings(max_examples=50, derandomize=True, deadline=500)
@given(prefix=_safe_text, span=_safe_span_text, suffix=_safe_text, corrupt=st.booleans())
def test_ac3_ac4_property_claim_node_kept_iff_span_is_literal_substring(prefix, span, suffix, corrupt):
    synthesis_text = prefix + span + suffix
    test_span = span + _SENTINEL if corrupt else span
    response = {"nodes": [{"id": "claim:x", "type": "claim", "label": "L", "source_span": test_span}], "edges": []}

    kept_nodes, _, dropped_nodes, _ = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    if corrupt:
        assert kept_nodes == []
        assert dropped_nodes == 1
    else:
        assert len(kept_nodes) == 1
        assert kept_nodes[0].source_span == span
        assert dropped_nodes == 0


# ---------------------------------------------------------------------------
# AC5: Given a concept-type node with no source_span at all, When it runs,
# Then it is kept without a span check.
# ---------------------------------------------------------------------------


def test_ac5_concept_node_without_source_span_key_is_kept():
    synthesis_text = "Text that is entirely irrelevant to the concept."
    response = {"nodes": [{"id": "c1", "type": "concept", "label": "Concept"}], "edges": []}
    kept_nodes, _, dropped_nodes, _ = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert len(kept_nodes) == 1
    assert kept_nodes[0].type == "concept"
    assert dropped_nodes == 0


def test_ac5_concept_node_with_explicit_null_source_span_is_kept():
    synthesis_text = "Text that is entirely irrelevant to the concept."
    response = {"nodes": [{"id": "c1", "type": "concept", "label": "Concept", "source_span": None}], "edges": []}
    kept_nodes, _, dropped_nodes, _ = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert len(kept_nodes) == 1
    assert kept_nodes[0].source_span is None
    assert dropped_nodes == 0


# ---------------------------------------------------------------------------
# AC6: Given a supports/contradicts/derives-from edge whose source_span is
# not a literal substring of synthesis_text, OR whose from/to does not
# resolve to a known node id, When it runs, Then that edge is dropped and
# dropped_edge_count increments -- same drop-and-count rule as node
# validation, no exceptions for edges.
# ---------------------------------------------------------------------------


def test_ac6_edge_dropped_when_source_span_not_in_synthesis():
    synthesis_text = "The Eiffel Tower is 330 meters tall and was completed in 1889."
    response = {
        "nodes": [{"id": "claim:1", "type": "claim", "label": "L", "source_span": "330 meters tall"}],
        "edges": [
            {
                "id": "e1",
                "from_id": "claim:1",
                "to_id": "ref:1",
                "type": "supports",
                "source_span": "this text is not present anywhere",
            }
        ],
    }
    kept_nodes, kept_edges, _, dropped_edges = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids={"ref:1"}
    )
    assert kept_edges == []
    assert dropped_edges == 1
    assert len(kept_nodes) == 1  # node validity is independent of edge validity


def test_ac6_edge_dropped_when_endpoint_unresolved():
    synthesis_text = "The Eiffel Tower is 330 meters tall and was completed in 1889."
    response = {
        "nodes": [{"id": "claim:1", "type": "claim", "label": "L", "source_span": "330 meters tall"}],
        "edges": [
            {
                "id": "e1",
                "from_id": "claim:1",
                "to_id": "ref:DOES_NOT_EXIST",
                "type": "derives-from",
                "source_span": "330 meters tall",
            }
        ],
    }
    kept_nodes, kept_edges, _, dropped_edges = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids={"ref:1"}
    )
    assert kept_edges == []
    assert dropped_edges == 1
    assert len(kept_nodes) == 1


def test_ac6_relates_to_and_cites_edges_exempt_from_span_check():
    # Grounded directly in the GraphEdge dataclass comment: source_span is
    # "required for supports/contradicts/derives-from, None for
    # relates-to/cites" -- the edge-side analogue of AC5's concept-node
    # exemption.
    synthesis_text = "Some synthesis text here."
    response = {
        "nodes": [{"id": "c1", "type": "concept", "label": "C"}],
        "edges": [{"id": "e1", "from_id": "c1", "to_id": "c1", "type": "relates-to", "source_span": None}],
    }
    _, kept_edges, _, dropped_edges = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert len(kept_edges) == 1
    assert dropped_edges == 0


@settings(max_examples=50, derandomize=True, deadline=500)
@given(
    prefix=_safe_text,
    span=_safe_span_text,
    suffix=_safe_text,
    edge_type=st.sampled_from(["supports", "contradicts", "derives-from"]),
    corrupt_span=st.booleans(),
)
def test_ac6_property_edge_kept_iff_span_valid_when_endpoints_resolve(prefix, span, suffix, edge_type, corrupt_span):
    synthesis_text = prefix + span + suffix
    test_span = span + _SENTINEL if corrupt_span else span
    response = {
        "nodes": [{"id": "claim:x", "type": "claim", "label": "L", "source_span": span}],
        "edges": [{"id": "e:x", "from_id": "claim:x", "to_id": "ref:1", "type": edge_type, "source_span": test_span}],
    }
    _, kept_edges, _, dropped_edges = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids={"ref:1"}
    )
    if corrupt_span:
        assert kept_edges == []
        assert dropped_edges == 1
    else:
        assert len(kept_edges) == 1
        assert dropped_edges == 0


# ---------------------------------------------------------------------------
# Mutation-gate hardening (2026-08-13) -- scoped mutmut run on
# parse_and_validate_extraction surfaced that every dropped_node_count/
# dropped_edge_count increment site was only ever exercised with a SINGLE
# drop in the whole response, and never followed by another item in the
# same loop. That leaves `+= 1` indistinguishable from a bare `= 1`
# (overwrite instead of accumulate) and `continue` indistinguishable from
# `break` (stops the loop instead of moving to the next item). Each test
# below drops >=2 items via the SAME code path and includes one valid item
# AFTER them, so both the exact count and continued iteration are pinned.
# ---------------------------------------------------------------------------


def test_dropped_node_count_accumulates_and_loop_continues_past_malformed_nodes():
    response = {
        "nodes": [
            {"id": "", "type": "concept"},  # malformed: empty id -> _coerce_node None
            {"type": "concept"},  # malformed: missing id -> _coerce_node None
            {"id": "c1", "type": "concept", "label": "kept"},
        ],
        "edges": [],
    }
    kept_nodes, _, dropped_node_count, _ = parse_and_validate_extraction(
        json.dumps(response), "text", known_node_ids=set()
    )
    assert dropped_node_count == 2
    assert [n.id for n in kept_nodes] == ["c1"]


def test_dropped_node_count_accumulates_and_loop_continues_past_bad_claim_spans():
    synthesis_text = "The real text."
    response = {
        "nodes": [
            {"id": "cl1", "type": "claim", "label": "L", "source_span": "not present"},
            {"id": "cl2", "type": "claim", "label": "L", "source_span": "also absent"},
            {"id": "c1", "type": "concept", "label": "kept"},
        ],
        "edges": [],
    }
    kept_nodes, _, dropped_node_count, _ = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert dropped_node_count == 2
    assert [n.id for n in kept_nodes] == ["c1"]


def test_dropped_edge_count_accumulates_and_loop_continues_past_malformed_edges():
    response = {
        "nodes": [{"id": "c1", "type": "concept", "label": "L"}],
        "edges": [
            {"id": "", "from_id": "c1", "to_id": "c1", "type": "relates-to"},  # malformed: empty id
            {"from_id": "c1", "to_id": "c1", "type": "relates-to"},  # malformed: missing id
            {"id": "e1", "from_id": "c1", "to_id": "c1", "type": "relates-to", "source_span": None},
        ],
    }
    _, kept_edges, _, dropped_edge_count = parse_and_validate_extraction(
        json.dumps(response), "text", known_node_ids=set()
    )
    assert dropped_edge_count == 2
    assert [e.id for e in kept_edges] == ["e1"]


def test_dropped_edge_count_accumulates_and_loop_continues_past_unresolved_endpoints():
    response = {
        "nodes": [{"id": "c1", "type": "concept", "label": "L"}],
        "edges": [
            {"id": "e1", "from_id": "MISSING1", "to_id": "c1", "type": "relates-to", "source_span": None},
            {"id": "e2", "from_id": "MISSING2", "to_id": "c1", "type": "relates-to", "source_span": None},
            {"id": "e3", "from_id": "c1", "to_id": "c1", "type": "relates-to", "source_span": None},
        ],
    }
    _, kept_edges, _, dropped_edge_count = parse_and_validate_extraction(
        json.dumps(response), "text", known_node_ids=set()
    )
    assert dropped_edge_count == 2
    assert [e.id for e in kept_edges] == ["e3"]


def test_dropped_edge_count_accumulates_and_loop_continues_past_bad_edge_spans():
    synthesis_text = "The real text."
    response = {
        "nodes": [{"id": "c1", "type": "concept", "label": "L"}],
        "edges": [
            {"id": "e1", "from_id": "c1", "to_id": "c1", "type": "supports", "source_span": "absent one"},
            {"id": "e2", "from_id": "c1", "to_id": "c1", "type": "supports", "source_span": "absent two"},
            {"id": "e3", "from_id": "c1", "to_id": "c1", "type": "relates-to", "source_span": None},
        ],
    }
    _, kept_edges, _, dropped_edge_count = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert dropped_edge_count == 2
    assert [e.id for e in kept_edges] == ["e3"]


def test_malformed_json_branch_returns_exact_zero_counts():
    # DOCUMENTED ASSUMPTIONS #2 deliberately does not pin this as a
    # CONTRACT guarantee (the docstring calls these counts "unreliable"
    # for this branch). This test instead pins it as CURRENT-BEHAVIOR
    # regression protection only (a scoped mutmut run found `return [], [],
    # 0, 0` silently degrading to `1, 0` / `0, 1` with zero test coverage)
    # -- if a future change deliberately makes this branch return nonzero
    # counts, update/remove this test, not the implementation.
    kept_nodes, kept_edges, dropped_node_count, dropped_edge_count = parse_and_validate_extraction(
        "not json {{{", "text", known_node_ids=set()
    )
    assert (kept_nodes, kept_edges, dropped_node_count, dropped_edge_count) == ([], [], 0, 0)


# ---------------------------------------------------------------------------
# AC7: Given the LLM response is malformed (not valid JSON, or valid JSON
# missing the expected nodes/edges keys), When parse_and_validate_extraction
# runs, Then it returns an empty nodes/edges list and the caller
# (build_reasoning_graph) surfaces this distinctly (via skip_reason) rather
# than silently returning a graph containing only the deterministic
# reference half.
# ---------------------------------------------------------------------------


def test_ac7_malformed_json_text_returns_empty_node_and_edge_lists():
    kept_nodes, kept_edges, _, _ = parse_and_validate_extraction(
        "this is not json at all {{{", "some synthesis text", known_node_ids=set()
    )
    assert kept_nodes == []
    assert kept_edges == []


def test_ac7_valid_json_missing_nodes_and_edges_keys_returns_empty_lists():
    kept_nodes, kept_edges, _, _ = parse_and_validate_extraction(
        json.dumps({"unexpected": "shape"}), "some synthesis text", known_node_ids=set()
    )
    assert kept_nodes == []
    assert kept_edges == []


def test_ac7_valid_nodes_list_but_edges_not_a_list_is_rejected():
    # Mutation-gate hardening (2026-08-13): flipping the "or" to "and" in
    # `not isinstance(nodes, list) or not isinstance(edges, list)` survived
    # because no test had exactly ONE of the two keys be a valid list --
    # both must independently reject.
    kept_nodes, kept_edges, _, _ = parse_and_validate_extraction(
        json.dumps({"nodes": [], "edges": "not-a-list"}), "some synthesis text", known_node_ids=set()
    )
    assert kept_nodes == []
    assert kept_edges == []


def test_ac7_parse_and_validate_extraction_never_raises_on_malformed_input():
    for bad_input in ("{not valid json", "null", "[]", "", "12345", '{"nodes": "not-a-list", "edges": []}'):
        try:
            parse_and_validate_extraction(bad_input, "text", known_node_ids=set())
        except Exception as exc:  # noqa: BLE001 -- explicitly testing "never raises"
            raise AssertionError(
                f"parse_and_validate_extraction raised {exc!r} on input {bad_input!r}; "
                "contract requires it never raise"
            )


def test_ac7_build_reasoning_graph_surfaces_malformed_response_as_no_partial_graph():
    facts = [_fact("1", "some fact")]
    query_fn = _query_fn_returning("not valid json at all {{{")

    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="run-7",
            synthesis_text="The synthesis text.",
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="test-model",
            query_fn=query_fn,
            timeout=30.0,
        )
    )

    # Never silently return a graph containing only the deterministic
    # reference half with no indication the LLM half failed.
    assert graph is None
    assert skip_reason is not None
    assert isinstance(skip_reason, str) and skip_reason != ""


# ---------------------------------------------------------------------------
# AC8: Given query_fn raises (network error) or returns None, When
# build_reasoning_graph runs, Then it returns (None, "extraction_error") --
# the deterministic reference nodes/edges are NOT persisted on their own;
# no exception propagates to the caller.
# ---------------------------------------------------------------------------


def test_ac8_query_fn_exception_returns_none_graph_and_extraction_error():
    facts = [_fact("1", "some fact")]
    query_fn = _query_fn_raising(RuntimeError("simulated network failure"))

    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="run-8a",
            synthesis_text="text",
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="test-model",
            query_fn=query_fn,
            timeout=30.0,
        )
    )
    assert graph is None
    assert skip_reason == "extraction_error"


def test_ac8_query_fn_returning_none_returns_none_graph_and_extraction_error():
    facts = [_fact("1", "some fact")]
    query_fn = _query_fn_returning_none()

    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="run-8b",
            synthesis_text="text",
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="test-model",
            query_fn=query_fn,
            timeout=30.0,
        )
    )
    assert graph is None
    assert skip_reason == "extraction_error"


def test_build_reasoning_graph_invariant_skip_reason_none_iff_graph_present():
    # Docstring law: "skip_reason is None iff graph is not None" -- checked
    # across every failure/success branch this contract defines.
    facts = [_fact("1", "some fact")]
    synthesis_text = "The synthesis text."
    scenarios = [
        _query_fn_returning(json.dumps({"nodes": [], "edges": []})),
        _query_fn_returning("not json"),
        _query_fn_raising(RuntimeError("boom")),
        _query_fn_returning_none(),
    ]
    for query_fn in scenarios:
        graph, skip_reason = asyncio.run(
            build_reasoning_graph(
                run_id="run-inv",
                synthesis_text=synthesis_text,
                verified_facts=facts,
                dropped_fact_ids=set(),
                model="test-model",
                query_fn=query_fn,
                timeout=30.0,
            )
        )
        assert (graph is not None) == (skip_reason is None)


# ---------------------------------------------------------------------------
# AC9: Given a successful extraction, When build_reasoning_graph returns,
# Then the returned ReasoningGraph.nodes is the deterministic reference set
# UNION the validated concept/claim set, and .edges is the deterministic
# cites set UNION the validated relationship set.
# ---------------------------------------------------------------------------


def test_ac9_successful_extraction_merges_deterministic_and_llm_halves():
    facts = [_fact("1", "Paris is the capital of France")]
    synthesis_text = "France's capital, Paris, has been the seat of government since medieval times."
    response = {
        "nodes": [
            {
                "id": "claim:a",
                "type": "claim",
                "label": "Capital claim",
                "source_span": "seat of government since medieval times",
            }
        ],
        "edges": [
            {"id": "e:cite", "from_id": "claim:a", "to_id": "ref:1", "type": "cites", "source_span": None},
        ],
    }
    query_fn = _query_fn_returning(json.dumps(response))

    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="run-9",
            synthesis_text=synthesis_text,
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="test-model",
            query_fn=query_fn,
            timeout=30.0,
        )
    )

    assert skip_reason is None
    assert graph is not None
    node_ids = {n.id for n in graph.nodes}
    assert "ref:1" in node_ids  # deterministic half present
    assert "claim:a" in node_ids  # validated LLM half present

    ref_cites_edge_present = any(
        e.type == "cites" and "ref:1" in (e.from_id, e.to_id) for e in graph.edges
    )
    llm_edge_present = any(e.from_id == "claim:a" and e.to_id == "ref:1" for e in graph.edges)
    assert ref_cites_edge_present
    assert llm_edge_present


# ---------------------------------------------------------------------------
# AC10: Given a ReasoningGraph, When render_markdown runs, Then the output
# is a fenced ```mermaid block using graph TD syntax, every node rendered
# with a shape keyed to its type (visually distinct per type), every edge
# labeled with its type, and the output ends with the fixed disclaimer
# sentence verbatim, every time, regardless of graph content.
# ---------------------------------------------------------------------------


def test_ac10_render_markdown_uses_fenced_mermaid_graph_td_block_and_labels_edges():
    graph = _graph(
        nodes=[
            GraphNode(id="c1", type="concept", label="Concept A", source_span=None, origin="stage3:synthesis"),
            GraphNode(id="cl1", type="claim", label="Claim A", source_span="span text", origin="stage3:synthesis"),
            GraphNode(id="ref:1", type="reference", label="Ref A", source_span="fact text", origin="verified_facts:1"),
        ],
        edges=[
            GraphEdge(id="e1", from_id="cl1", to_id="c1", type="relates-to", source_span=None),
            GraphEdge(id="e2", from_id="cl1", to_id="ref:1", type="cites", source_span=None),
        ],
    )
    md = render_markdown(graph)

    assert "```mermaid" in md
    assert "graph TD" in md
    assert "relates-to" in md
    assert "cites" in md


def test_ac10_render_markdown_distinguishes_node_shapes_by_type():
    graph = _graph(
        nodes=[
            GraphNode(id="c1", type="concept", label="UNIQUELABELCONCEPT", source_span=None, origin="stage3:synthesis"),
            GraphNode(id="cl1", type="claim", label="UNIQUELABELCLAIM", source_span="s", origin="stage3:synthesis"),
            GraphNode(id="ref1", type="reference", label="UNIQUELABELREF", source_span="s", origin="verified_facts:1"),
        ],
        edges=[],
    )
    md = render_markdown(graph)

    def _wrapper(label: str) -> str:
        idx = md.index(label)
        start = max(0, idx - 6)
        end = min(len(md), idx + len(label) + 6)
        return md[start:end]

    concept_shape = _wrapper("UNIQUELABELCONCEPT")
    claim_shape = _wrapper("UNIQUELABELCLAIM")
    ref_shape = _wrapper("UNIQUELABELREF")

    assert concept_shape != claim_shape
    assert claim_shape != ref_shape
    assert concept_shape != ref_shape


def test_ac10_render_markdown_ends_with_verbatim_disclaimer():
    empty_graph = _graph(nodes=[], edges=[])
    full_graph = _graph(
        nodes=[GraphNode(id="c1", type="concept", label="X", source_span=None, origin="stage3:synthesis")],
        edges=[],
    )
    for graph in (empty_graph, full_graph):
        md = render_markdown(graph)
        assert md.rstrip().endswith(DISCLAIMER)


@settings(max_examples=50, derandomize=True, deadline=500)
@given(n_concepts=st.integers(min_value=0, max_value=4), n_claims=st.integers(min_value=0, max_value=4))
def test_ac10_property_disclaimer_present_verbatim_regardless_of_graph_size(n_concepts, n_claims):
    nodes = [
        GraphNode(id=f"c{i}", type="concept", label=f"Concept {i}", source_span=None, origin="stage3:synthesis")
        for i in range(n_concepts)
    ] + [
        GraphNode(id=f"cl{i}", type="claim", label=f"Claim {i}", source_span=f"span{i}", origin="stage3:synthesis")
        for i in range(n_claims)
    ]
    graph = _graph(nodes=nodes, edges=[])
    md = render_markdown(graph)
    assert md.rstrip().endswith(DISCLAIMER)


# ---------------------------------------------------------------------------
# Mutation-gate hardening (2026-08-13) -- scoped mutmut run on this file
# surfaced several render_markdown gaps the tests above don't reach: the
# exact opening/closing lines, that node ids/edge endpoints are actually run
# through _sanitize_mermaid_id (not silently dropped to "None"), that a
# `"` in a label is actually replaced with `'`, the exact edge-arrow syntax,
# and the (currently untested, dict-lookup-with-default) fallback shape for
# a node.type outside the three known literals. NOT pinning the literal
# bracket characters per-type -- that stays deliberately unpinned per this
# file's own DOCUMENTED ASSUMPTIONS #4 (the contract's own wording is
# inconsistent about them).
# ---------------------------------------------------------------------------


def test_render_markdown_exact_structural_lines_and_sanitization():
    graph = _graph(
        nodes=[
            GraphNode(
                id="c:1", type="concept", label='Has "quotes" in it',
                source_span=None, origin="stage3:synthesis",
            ),
        ],
        edges=[
            GraphEdge(id="e:1", from_id="c:1", to_id="c:1", type="relates-to", source_span=None),
        ],
    )
    md = render_markdown(graph)
    lines = md.split("\n")

    # Exact opening two lines (mermaid fence + graph-direction header).
    assert lines[0] == "```mermaid"
    assert lines[1] == "graph TD"

    # Node id "c:1" must be sanitized (":" -> "_") wherever it is rendered,
    # and the label's embedded `"` must become `'` -- both silently break
    # (render as the raw id / raw quote, or as the literal string "None")
    # under the surviving mutants this closes.
    node_line = lines[2]
    assert "None" not in node_line
    assert "c_1" in node_line
    assert "Has 'quotes' in it" in node_line

    # Edge line: exact arrow syntax between sanitized endpoints.
    edge_line = lines[3]
    assert edge_line == "    c_1 -->|relates-to| c_1"

    # Exact closing three lines: fence, blank, disclaimer.
    assert lines[-3] == "```"
    assert lines[-2] == ""
    assert lines[-1] == DISCLAIMER


def test_sanitize_mermaid_id_preserves_both_letter_cases_and_maps_punctuation():
    # Direct unit test for scripts.reasoning_graph._sanitize_mermaid_id --
    # a scoped mutmut run found the char-class's upper/lowercase ranges
    # (A-Z and a-z, both required) were unverified: mutating either range
    # to duplicate the other (e.g. "[^a-za-z0-9_]", dropping the uppercase
    # range) survived, since no prior id fixture mixed both cases.
    result = rg._sanitize_mermaid_id("Ab:c 9_Z")
    assert result == "Ab_c_9_Z"


def test_coerce_node_rejects_non_string_truthy_id():
    # Mutation-gate hardening (2026-08-13): flipping the "or" to "and" in
    # `not isinstance(node_id, str) or not node_id` survived because no
    # test exercised a non-string but TRUTHY id (e.g. an int) -- both
    # conditions must independently reject, not only their conjunction.
    assert rg._coerce_node({"id": 123, "type": "concept", "label": "L"}) is None


def test_coerce_node_missing_label_defaults_to_empty_string():
    node = rg._coerce_node({"id": "n1", "type": "concept"})
    assert node.label == ""


def test_coerce_node_string_label_preserved_verbatim():
    node = rg._coerce_node({"id": "n1", "type": "concept", "label": "a real label"})
    assert node.label == "a real label"


def test_coerce_node_non_string_non_none_label_is_str_coerced():
    node = rg._coerce_node({"id": "n1", "type": "concept", "label": 123})
    assert node.label == "123"


def test_coerce_node_label_truncated_at_exactly_80_chars():
    node = rg._coerce_node({"id": "n1", "type": "concept", "label": "Y" * 100})
    assert node.label == "Y" * 80
    assert len(node.label) == 80


def test_coerce_node_non_string_source_span_is_coerced_to_none():
    # Mutation-gate hardening: an invalid (non-str, non-None) source_span
    # must be nulled out, not passed through raw.
    node = rg._coerce_node({"id": "n1", "type": "claim", "label": "L", "source_span": 123})
    assert node.source_span is None


def test_coerce_edge_rejects_non_string_truthy_id():
    assert (
        rg._coerce_edge({"id": 123, "from_id": "a", "to_id": "b", "type": "relates-to"})
        is None
    )


def test_coerce_edge_rejects_when_only_one_endpoint_is_non_string():
    # Mutation-gate hardening: flipping "or" to "and" in the from/to
    # isinstance check survived because no test had exactly ONE bad
    # endpoint -- both must independently reject.
    assert (
        rg._coerce_edge({"id": "e1", "from_id": 123, "to_id": "b", "type": "relates-to"})
        is None
    )
    assert (
        rg._coerce_edge({"id": "e1", "from_id": "a", "to_id": 123, "type": "relates-to"})
        is None
    )


def test_coerce_edge_non_string_source_span_is_coerced_to_none():
    edge = rg._coerce_edge(
        {"id": "e1", "from_id": "a", "to_id": "b", "type": "supports", "source_span": 123}
    )
    assert edge.source_span is None


def test_render_markdown_unknown_node_type_falls_back_to_bracket_default():
    # node.type is only a Literal at type-check time -- nothing stops a
    # runtime value outside {"concept","claim","reference"} from reaching
    # here. This exercises the dict.get(..., default) fallback branch
    # directly, which no other test reaches (all fixtures use valid types).
    graph = _graph(
        nodes=[
            GraphNode(
                id="n1", type="unknown_type", label="L",  # type: ignore[arg-type]
                source_span=None, origin="stage3:synthesis",
            ),
        ],
        edges=[],
    )
    md = render_markdown(graph)
    node_line = md.split("\n")[2]
    assert node_line == '    n1["L"]'


# ---------------------------------------------------------------------------
# AC11: Given output_dir already exists, When write_reasoning_graph_files
# runs, Then it writes exactly 3 files (reasoning_graph.json,
# reasoning_graph.md, synthesis.md) into that directory and returns their
# paths -- synthesis.md contains synthesis_text verbatim, no additional
# wrapping.
# ---------------------------------------------------------------------------


def test_ac11_writes_exactly_three_named_files_into_existing_output_dir(tmp_path):
    output_dir = tmp_path / "run-out"
    output_dir.mkdir()  # pre-existing, per contract's stated precondition
    graph = _graph(
        nodes=[GraphNode(id="ref:1", type="reference", label="R", source_span="fact", origin="verified_facts:1")],
        edges=[],
    )
    synthesis_text = "The final synthesis text.\nWith a newline and *markdown* chars."

    paths = write_reasoning_graph_files(output_dir, graph, synthesis_text)

    assert len(paths) == 3
    names = {p.name for p in paths}
    assert names == {"reasoning_graph.json", "reasoning_graph.md", "synthesis.md"}
    for p in paths:
        assert p.parent == output_dir
        assert p.exists()

    synthesis_path = next(p for p in paths if p.name == "synthesis.md")
    assert synthesis_path.read_text() == synthesis_text  # verbatim, no wrapping


def test_ac11_never_raises_for_pre_existing_output_dir(tmp_path):
    output_dir = tmp_path  # tmp_path is always pre-existing
    graph = _graph(nodes=[], edges=[])
    try:
        write_reasoning_graph_files(output_dir, graph, "synthesis text")
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"write_reasoning_graph_files raised {exc!r} for a pre-existing dir")


# ---------------------------------------------------------------------------
# Mutation-gate hardening (2026-08-13) -- scoped mutmut run surfaced two
# write_reasoning_graph_files gaps: mkdir's parents=True is defensive
# hardening beyond the stated precondition (output_dir already exists) but
# was never actually exercised on a genuinely multi-level-missing path, and
# the persisted JSON's indent=2 pretty-printing (a deliberate human-legible
# choice) was unverified since json.loads is indent-agnostic.
# ---------------------------------------------------------------------------


def test_write_reasoning_graph_files_creates_missing_parent_directories(tmp_path):
    output_dir = tmp_path / "does" / "not" / "exist" / "yet"
    graph = _graph(nodes=[], edges=[])
    write_reasoning_graph_files(output_dir, graph, "text")
    assert output_dir.is_dir()
    assert (output_dir / "synthesis.md").exists()


def test_write_reasoning_graph_files_json_is_pretty_printed_with_2_space_indent(tmp_path):
    output_dir = tmp_path / "run-out"
    output_dir.mkdir()
    graph = _graph(
        nodes=[GraphNode(id="ref:1", type="reference", label="R", source_span="fact", origin="verified_facts:1")],
        edges=[],
    )
    paths = write_reasoning_graph_files(output_dir, graph, "text")
    json_path = next(p for p in paths if p.name == "reasoning_graph.json")
    raw = json_path.read_text()
    assert raw.startswith('{\n  "')  # indent=2: newline + exactly 2 spaces after the opening brace


# ---------------------------------------------------------------------------
# AC12: Given the full ReasoningGraph dataclass, When serialized to
# reasoning_graph.json, Then the JSON is round-trippable and includes
# schema_version, run_id, generated_by, generated_at, dropped_node_count,
# dropped_edge_count at the top level, alongside nodes/edges.
# ---------------------------------------------------------------------------


def test_ac12_reasoning_graph_json_round_trips_with_required_top_level_keys(tmp_path):
    output_dir = tmp_path / "run-out"
    output_dir.mkdir()
    graph = _graph(
        schema_version="1.0",
        run_id="run-12",
        generated_by="test-model-x",
        generated_at="2026-08-13T12:00:00Z",
        nodes=[GraphNode(id="ref:1", type="reference", label="R", source_span="fact", origin="verified_facts:1")],
        edges=[GraphEdge(id="e1", from_id="ref:1", to_id="ref:1", type="cites", source_span=None)],
        dropped_node_count=2,
        dropped_edge_count=1,
    )

    paths = write_reasoning_graph_files(output_dir, graph, "text")
    json_path = next(p for p in paths if p.name == "reasoning_graph.json")
    loaded = json.loads(json_path.read_text())

    for key in (
        "schema_version",
        "run_id",
        "generated_by",
        "generated_at",
        "dropped_node_count",
        "dropped_edge_count",
        "nodes",
        "edges",
    ):
        assert key in loaded

    assert loaded["schema_version"] == "1.0"
    assert loaded["run_id"] == "run-12"
    assert loaded["generated_by"] == "test-model-x"
    assert loaded["generated_at"] == "2026-08-13T12:00:00Z"
    assert loaded["dropped_node_count"] == 2
    assert loaded["dropped_edge_count"] == 1
    assert len(loaded["nodes"]) == 1
    assert len(loaded["edges"]) == 1


@settings(max_examples=30, derandomize=True, deadline=1000)
@given(
    run_id=st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=20),
    dropped_node_count=st.integers(min_value=0, max_value=50),
    dropped_edge_count=st.integers(min_value=0, max_value=50),
)
def test_ac12_property_json_round_trip_preserves_top_level_scalars(
    tmp_path_factory, run_id, dropped_node_count, dropped_edge_count
):
    output_dir = tmp_path_factory.mktemp("rg")
    graph = _graph(
        run_id=run_id,
        dropped_node_count=dropped_node_count,
        dropped_edge_count=dropped_edge_count,
        nodes=[],
        edges=[],
    )
    paths = write_reasoning_graph_files(output_dir, graph, "synthesis")
    json_path = next(p for p in paths if p.name == "reasoning_graph.json")
    loaded = json.loads(json_path.read_text())

    assert loaded["run_id"] == run_id
    assert loaded["dropped_node_count"] == dropped_node_count
    assert loaded["dropped_edge_count"] == dropped_edge_count


# ---------------------------------------------------------------------------
# Signature-derived (not a numbered AC): build_extraction_prompt "One
# prompt, synthesis text only" -- the prompt actually carries the synthesis
# text it is meant to extract from.
# ---------------------------------------------------------------------------


def test_build_extraction_prompt_contains_synthesis_text_verbatim():
    synthesis_text = "A UNIQUE_MARKER_STRING_998877 sentence for prompt-inclusion testing."
    prompt = build_extraction_prompt(synthesis_text, reference_node_ids=["ref:1", "ref:2"])
    assert "UNIQUE_MARKER_STRING_998877" in prompt


# ===========================================================================
# Mutation-gate hardening (2026-08-13). Everything below was added AFTER the
# blind acceptance suite above, in response to a scoped mutmut run against
# this file's changed surface (163 initial survivors project-wide, 143 of
# them in this file). Each test below targets a specific class of survivor:
# static template text, private coercion helpers never exercised by an exact
# assertion, and exact-count/field checks the blind ACs deliberately left
# loose. One documented TRUE equivalent mutant remains (see the comment on
# `_try_parse_extraction_json`'s `or`/`and` line above) -- everything else
# is now killed by an assertion below.
# ===========================================================================


# ---------------------------------------------------------------------------
# build_extraction_prompt: the blind test above only checks that the
# synthesis text appears somewhere in the prompt -- every other word of the
# static template (rules, JSON-shape example, ref_ids_block join/fallback)
# was unguarded, so mutmut could freely reword it undetected. Pinned exactly
# since this is model-facing instruction text, not a paraphrase-tolerant
# free-form string -- a silent wording regression here changes what the
# extraction model is told to do.
# ---------------------------------------------------------------------------


def test_build_extraction_prompt_exact_content_with_reference_ids():
    prompt = build_extraction_prompt("SYNTH_TEXT_HERE", reference_node_ids=["ref:1", "ref:2"])
    assert prompt == (
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
        "ref:1, ref:2.\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no other "
        "text) in exactly this shape:\n"
        '{"nodes": [{"id": "...", "type": "concept"|"claim", '
        '"label": "...", "source_span": "..."|null}], '
        '"edges": [{"id": "...", "from_id": "...", "to_id": "...", '
        '"type": "relates-to"|"supports"|"contradicts"|"derives-from"|"cites", '
        '"source_span": "..."|null}]}\n\n'
        "Synthesis text:\n"
        "SYNTH_TEXT_HERE"
    )


def test_build_extraction_prompt_exact_content_with_no_reference_ids():
    prompt = build_extraction_prompt("X", reference_node_ids=[])
    assert prompt.count("(none)") == 1
    assert "ref:1" not in prompt
    assert prompt.endswith("Synthesis text:\nX")


# ---------------------------------------------------------------------------
# build_reference_nodes_and_edges: AC1/AC2 above check id/type/source_span/
# origin and edge-endpoint SET membership, but never the exact `label`
# (including its 80-char truncation) nor the exact edge `id`/`from_id`/
# `to_id` values -- a `from_id=None` edge still passes the AC1 set-membership
# check as long as `to_id` independently equals the node id.
# ---------------------------------------------------------------------------


def test_build_reference_nodes_and_edges_exact_label_and_edge_fields():
    facts = [_fact("1", "short claim text")]
    nodes, edges = build_reference_nodes_and_edges(facts, dropped_fact_ids=set())
    assert nodes[0].label == "short claim text"
    assert edges[0].id == "cites:1"
    assert edges[0].from_id == "ref:1"
    assert edges[0].to_id == "ref:1"


def test_build_reference_nodes_and_edges_label_truncated_to_exactly_80_chars():
    long_text = "y" * 100
    facts = [_fact("1", long_text)]
    nodes, _ = build_reference_nodes_and_edges(facts, dropped_fact_ids=set())
    assert nodes[0].label == "y" * 80
    assert len(nodes[0].label) == 80


# ---------------------------------------------------------------------------
# _try_parse_extraction_json: the "or"/"and" mutant on
# `"nodes" not in data or "edges" not in data` is a documented TRUE
# equivalent (see the comment on that line -- the isinstance check
# independently rejects a missing key's None). This test targets the
# SECOND, DISTINCT `or`/`and` line -- `not isinstance(nodes, list) or not
# isinstance(edges, list)` -- which is NOT equivalent: with `and`, a dict
# where exactly one of nodes/edges is a non-list value would incorrectly
# pass through instead of being rejected.
# ---------------------------------------------------------------------------


def test_try_parse_extraction_json_rejects_when_only_one_key_is_a_list():
    assert _try_parse_extraction_json(json.dumps({"nodes": "not-a-list", "edges": []})) is None
    assert _try_parse_extraction_json(json.dumps({"nodes": [], "edges": "not-a-list"})) is None


# ---------------------------------------------------------------------------
# _coerce_node: never directly imported/tested by the blind suite (only
# exercised indirectly through parse_and_validate_extraction, where its
# label/None-coercion/truncation details are invisible to the ACs' looser
# assertions). Direct tests here pin every field exactly.
# ---------------------------------------------------------------------------


def test_coerce_node_full_field_mapping_for_valid_concept():
    node = _coerce_node({"id": "n1", "type": "concept", "label": "My Label", "source_span": "abc"})
    assert node.id == "n1"
    assert node.type == "concept"
    assert node.label == "My Label"
    assert node.source_span == "abc"
    assert node.origin == "stage3:synthesis"


def test_coerce_node_rejects_non_string_or_empty_id():
    assert _coerce_node({"id": 5, "type": "concept"}) is None
    assert _coerce_node({"id": "", "type": "concept"}) is None
    assert _coerce_node({"id": None, "type": "concept"}) is None


def test_coerce_node_rejects_invalid_type():
    assert _coerce_node({"id": "n1", "type": "reference"}) is None
    assert _coerce_node({"id": "n1", "type": "bogus"}) is None


def test_coerce_node_missing_label_defaults_to_empty_string_not_none():
    node = _coerce_node({"id": "n1", "type": "concept"})
    assert node.label == ""


def test_coerce_node_non_string_label_is_stringified():
    node = _coerce_node({"id": "n1", "type": "concept", "label": 42})
    assert node.label == "42"


def test_coerce_node_label_truncated_to_exactly_80_chars():
    node = _coerce_node({"id": "n1", "type": "concept", "label": "x" * 100})
    assert node.label == "x" * 80
    assert len(node.label) == 80


def test_coerce_node_non_string_source_span_coerced_to_none():
    node = _coerce_node({"id": "n1", "type": "concept", "source_span": 123})
    assert node.source_span is None


def test_coerce_node_valid_string_source_span_kept_verbatim():
    node = _coerce_node({"id": "n1", "type": "concept", "source_span": "verbatim span"})
    assert node.source_span == "verbatim span"


# ---------------------------------------------------------------------------
# _coerce_edge: same rationale as _coerce_node above.
# ---------------------------------------------------------------------------


def test_coerce_edge_full_field_mapping_for_valid_edge():
    edge = _coerce_edge(
        {"id": "e1", "from_id": "a", "to_id": "b", "type": "relates-to", "source_span": None}
    )
    assert edge.id == "e1"
    assert edge.from_id == "a"
    assert edge.to_id == "b"
    assert edge.type == "relates-to"
    assert edge.source_span is None


def test_coerce_edge_rejects_non_string_or_empty_id():
    assert _coerce_edge({"id": 5, "from_id": "a", "to_id": "b", "type": "relates-to"}) is None
    assert _coerce_edge({"id": "", "from_id": "a", "to_id": "b", "type": "relates-to"}) is None


def test_coerce_edge_rejects_non_string_from_id_or_to_id():
    assert _coerce_edge({"id": "e1", "from_id": 5, "to_id": "b", "type": "relates-to"}) is None
    assert _coerce_edge({"id": "e1", "from_id": "a", "to_id": 5, "type": "relates-to"}) is None


def test_coerce_edge_non_string_source_span_coerced_to_none():
    edge = _coerce_edge(
        {"id": "e1", "from_id": "a", "to_id": "b", "type": "cites", "source_span": 999}
    )
    assert edge.source_span is None


# ---------------------------------------------------------------------------
# _sanitize_mermaid_id: never directly tested; only observed indirectly
# through render_markdown's substring checks, which never distinguish a
# broken character class or a multi-char replacement from the real one.
# ---------------------------------------------------------------------------


def test_sanitize_mermaid_id_replaces_each_special_char_independently():
    assert _sanitize_mermaid_id("ref:1") == "ref_1"
    assert _sanitize_mermaid_id("a:b:c") == "a_b_c"
    assert _sanitize_mermaid_id("a b") == "a_b"


def test_sanitize_mermaid_id_leaves_alphanumeric_and_underscore_untouched():
    assert _sanitize_mermaid_id("UPPER_lower123") == "UPPER_lower123"


# ---------------------------------------------------------------------------
# parse_and_validate_extraction: AC3-AC7 each check ONE drop at a time, so
# `dropped_node_count += 1` reading as `= 1` (non-additive) or the
# drop-loop's `continue` reading as `break` (silently stops processing
# later items) both survive every existing test. This one scenario mixes
# multiple drops of every kind, in a non-last position, so both bugs change
# the final counts/kept-lists.
# ---------------------------------------------------------------------------


def test_parse_and_validate_extraction_counts_multiple_drops_exactly_and_processes_every_item():
    synthesis_text = "Alpha fact is here. Beta fact is here."
    response = {
        "nodes": [
            {"id": "bad1", "type": "invalid_type"},  # coerce failure #1
            {"id": "bad2", "type": "invalid_type"},  # coerce failure #2
            {"id": "c1", "type": "concept", "label": "kept concept"},  # kept (not last!)
            {"id": "claim1", "type": "claim", "label": "L", "source_span": "NOT PRESENT AT ALL"},
            {"id": "claim2", "type": "claim", "label": "L", "source_span": "ALSO NOT PRESENT"},
        ],
        "edges": [
            {"id": None},  # coerce failure #1
            {"id": None},  # coerce failure #2
            {"id": "e1", "from_id": "c1", "to_id": "MISSING", "type": "relates-to", "source_span": None},
            {"id": "e2", "from_id": "MISSING2", "to_id": "c1", "type": "relates-to", "source_span": None},
            {"id": "e3", "from_id": "c1", "to_id": "c1", "type": "supports", "source_span": "NOT IN TEXT"},
            {"id": "e4", "from_id": "c1", "to_id": "c1", "type": "contradicts", "source_span": "ALSO NOT IN TEXT"},
            {"id": "e5", "from_id": "c1", "to_id": "c1", "type": "relates-to", "source_span": None},  # kept, last
        ],
    }
    kept_nodes, kept_edges, dropped_node_count, dropped_edge_count = parse_and_validate_extraction(
        json.dumps(response), synthesis_text, known_node_ids=set()
    )
    assert dropped_node_count == 4
    assert [n.id for n in kept_nodes] == ["c1"]
    assert dropped_edge_count == 6
    assert [e.id for e in kept_edges] == ["e5"]


def test_parse_and_validate_extraction_malformed_input_returns_exact_zero_counts():
    kept_nodes, kept_edges, dropped_node_count, dropped_edge_count = parse_and_validate_extraction(
        "not json {{{", "text", known_node_ids=set()
    )
    assert (kept_nodes, kept_edges, dropped_node_count, dropped_edge_count) == ([], [], 0, 0)


# ---------------------------------------------------------------------------
# build_reasoning_graph: AC7-AC9 check presence/absence and set membership,
# never the exact args passed to query_fn, the exact skip_reason strings, or
# the ReasoningGraph's other top-level scalar fields (schema_version,
# generated_by, generated_at's format/timezone, dropped_*_count on the
# success path).
# ---------------------------------------------------------------------------


def test_build_reasoning_graph_calls_query_fn_with_expected_model_prompt_timeout():
    captured: dict = {}

    async def capturing_query_fn(model, prompt, timeout):
        captured["model"] = model
        captured["prompt"] = prompt
        captured["timeout"] = timeout
        return {"content": json.dumps({"nodes": [], "edges": []})}

    facts = [_fact("1", "some fact")]
    synthesis_text = "UNIQUE_SYNTHESIS_MARKER_X the synthesis."
    asyncio.run(
        build_reasoning_graph(
            run_id="run-cap",
            synthesis_text=synthesis_text,
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="model-xyz",
            query_fn=capturing_query_fn,
            timeout=42.5,
        )
    )
    assert captured["model"] == "model-xyz"
    assert captured["timeout"] == 42.5
    assert captured["prompt"] == build_extraction_prompt(synthesis_text, ["ref:1"])


def test_build_reasoning_graph_success_path_exact_top_level_fields():
    facts = [_fact("1", "fact one text")]
    synthesis_text = "Synthesis containing spanX literally."
    response = {
        "nodes": [{"id": "claim:a", "type": "claim", "label": "L", "source_span": "spanX"}],
        "edges": [],
    }
    query_fn = _query_fn_returning(json.dumps(response))

    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="run-exact",
            synthesis_text=synthesis_text,
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="model-exact",
            query_fn=query_fn,
            timeout=10.0,
        )
    )
    assert skip_reason is None
    assert graph.schema_version == rg.SCHEMA_VERSION
    assert graph.run_id == "run-exact"
    assert graph.generated_by == "model-exact"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", graph.generated_at)
    assert graph.dropped_node_count == 0
    assert graph.dropped_edge_count == 0


def test_build_reasoning_graph_generated_at_uses_utc_timezone(monkeypatch):
    captured_tz: dict = {}
    real_datetime = rg.datetime

    class FakeDatetime:
        @staticmethod
        def now(tz=None):
            captured_tz["tz"] = tz
            return real_datetime.now(tz)

    monkeypatch.setattr(rg, "datetime", FakeDatetime)
    facts = [_fact("1", "some fact")]
    query_fn = _query_fn_returning(json.dumps({"nodes": [], "edges": []}))
    asyncio.run(
        build_reasoning_graph(
            run_id="run-tz",
            synthesis_text="text",
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="m",
            query_fn=query_fn,
            timeout=30.0,
        )
    )
    assert captured_tz["tz"] is rg.timezone.utc


def test_build_reasoning_graph_content_not_string_returns_exact_extraction_error():
    async def bad_content_query_fn(model, prompt, timeout):
        return {"content": 12345}  # not a string

    facts = [_fact("1", "f")]
    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="r",
            synthesis_text="t",
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="m",
            query_fn=bad_content_query_fn,
            timeout=1.0,
        )
    )
    assert graph is None
    assert skip_reason == "extraction_error"


def test_build_reasoning_graph_malformed_response_returns_exact_skip_reason_string():
    facts = [_fact("1", "f")]
    query_fn = _query_fn_returning("not json at all {{{")
    graph, skip_reason = asyncio.run(
        build_reasoning_graph(
            run_id="r",
            synthesis_text="t",
            verified_facts=facts,
            dropped_fact_ids=set(),
            model="m",
            query_fn=query_fn,
            timeout=1.0,
        )
    )
    assert graph is None
    assert skip_reason == "malformed_extraction_response"


# ---------------------------------------------------------------------------
# render_markdown: AC10 above only checks substrings ("```mermaid" in md,
# etc.) and shape-distinctness by index-window comparison -- never the exact
# literal frame/arrow/label-quoting text, so mutmut could freely reword any
# of it (fence markers, the arrow syntax, the quote-replacement chars, the
# blank line before the disclaimer) without any test noticing.
# ---------------------------------------------------------------------------


def test_render_markdown_exact_content_for_a_small_graph():
    graph = _graph(
        nodes=[
            GraphNode(id="c1", type="concept", label='Has "quotes"', source_span=None, origin="stage3:synthesis"),
        ],
        edges=[
            GraphEdge(id="e1", from_id="c1", to_id="c1", type="relates-to", source_span=None),
        ],
    )
    md = render_markdown(graph)
    assert md == (
        "```mermaid\n"
        "graph TD\n"
        "    c1(\"Has 'quotes'\")\n"
        "    c1 -->|relates-to| c1\n"
        "```\n"
        "\n"
        f"{DISCLAIMER}"
    )


def test_render_markdown_sanitizes_punctuation_in_node_and_edge_refs():
    graph = _graph(
        nodes=[
            GraphNode(id="ref:1", type="reference", label="R", source_span="s", origin="verified_facts:1"),
        ],
        edges=[
            GraphEdge(id="e1", from_id="ref:1", to_id="ref:1", type="cites", source_span=None),
        ],
    )
    md = render_markdown(graph)
    assert "ref:1" not in md  # raw id (with the colon) never appears in the diagram body
    assert "ref_1" in md


# ---------------------------------------------------------------------------
# write_reasoning_graph_files: AC11/AC12 above use a pre-existing output_dir
# and never check the JSON's indentation or that missing PARENT directories
# get created -- `mkdir(parents=True, exist_ok=True)` degrading to
# `parents=False`/`None` only shows up once an ancestor is actually missing.
# ---------------------------------------------------------------------------


def test_write_reasoning_graph_files_creates_missing_parent_directories(tmp_path):
    output_dir = tmp_path / "a" / "b" / "run-out"  # multiple missing levels
    graph = _graph(nodes=[], edges=[])
    paths = write_reasoning_graph_files(output_dir, graph, "text")
    assert output_dir.exists()
    for p in paths:
        assert p.exists()


def test_write_reasoning_graph_files_json_uses_two_space_indent(tmp_path):
    output_dir = tmp_path / "run-out"
    output_dir.mkdir()
    graph = _graph(nodes=[], edges=[])
    paths = write_reasoning_graph_files(output_dir, graph, "text")
    json_path = next(p for p in paths if p.name == "reasoning_graph.json")
    raw = json_path.read_text()
    assert '\n  "schema_version"' in raw


# ---------------------------------------------------------------------------
# Mutation-gate hardening (2026-08-13) -- scoped mutmut run on this file
# surfaced that the substring-only check above leaves every fixed
# instruction/rule sentence in the prompt template (and the ref_ids_block
# join/empty-case logic) completely unverified. These two tests pin the
# COMPLETE, EXACT prompt text for a non-empty and an empty reference_node_ids
# input -- a golden-output check that fails on any wording/casing/ordering
# change to the template, closing that gap directly.
# ---------------------------------------------------------------------------


def test_build_extraction_prompt_exact_text_with_reference_ids():
    prompt = build_extraction_prompt("SYNTH_TEXT_X", ["ref:a", "ref:b"])
    assert prompt == (
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
        "ref:a, ref:b.\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no other "
        "text) in exactly this shape:\n"
        '{"nodes": [{"id": "...", "type": "concept"|"claim", '
        '"label": "...", "source_span": "..."|null}], '
        '"edges": [{"id": "...", "from_id": "...", "to_id": "...", '
        '"type": "relates-to"|"supports"|"contradicts"|"derives-from"|"cites", '
        '"source_span": "..."|null}]}\n\n'
        "Synthesis text:\n"
        "SYNTH_TEXT_X"
    )


def test_build_extraction_prompt_exact_text_with_no_reference_ids():
    prompt = build_extraction_prompt("SYNTH_TEXT_Y", [])
    assert prompt == (
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
        "(none).\n\n"
        "Respond with ONLY a JSON object (no markdown fences, no other "
        "text) in exactly this shape:\n"
        '{"nodes": [{"id": "...", "type": "concept"|"claim", '
        '"label": "...", "source_span": "..."|null}], '
        '"edges": [{"id": "...", "from_id": "...", "to_id": "...", '
        '"type": "relates-to"|"supports"|"contradicts"|"derives-from"|"cites", '
        '"source_span": "..."|null}]}\n\n'
        "Synthesis text:\n"
        "SYNTH_TEXT_Y"
    )

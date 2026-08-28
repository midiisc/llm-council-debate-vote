# Upstream issue — FILED as amiable-dev/llm-council#674

Filed 2026-08-28: https://github.com/amiable-dev/llm-council/issues/674

Drafted 2026-08-28, in a different project's session (Claude Code, `high-stakes-research-pipeline`
skill work) that was checking whether this repo's already-documented bias mitigations
(anonymization, shuffle, style-normalization) leave any real gap versus recent LLM-judge-bias
literature. Follows this project's own precedent (`#591`-`#596` in `docs/upstream-deltas.md`) —
grounded by direct read of the installed `llm-council-core` source, not inferred from the paper
alone. Filed with explicit user go-ahead ("file a bug if it makes sense and if it's in the
original repository").

---

**Title:** Self-preference recusal is not currently possible — `stage2_collect_rankings` sends
every reviewer the full anonymized batch in one call, so a model whose own response is in the
batch cannot be excluded from judging it without excluding it from reviewing anything that round

**Version:** llm-council-core 0.42.0 (PyPI), confirmed against the installed wheel at
`llm_council/council_stages.py`

**Summary:**

Panickssery et al., "LLM Evaluators Recognize and Favor Their Own Generations"
(arXiv:2404.13076), found LLM self-preference bias is partly driven by genuine self-recognition
— a model can sometimes tell its own output apart from others' even when identity is hidden
(anonymized labels, shuffled order, normalized style), and self-preference strength correlates
with self-recognition capability. This repo already implements every mitigation from the
anonymization/shuffle/style-normalization family (`docs/specs/stage3-chairman-anonymization-contract.md`,
`council_stages.py::stage1_5_normalize_styles`) — but none of those structurally *prevent* a
model from judging its own contribution, they only make that contribution harder to identify.
The complementary mitigation — excluding a model from judging any batch that contains its own
response — is not currently possible given how Stage 2 is structured.

**Root cause:**

`stage2_collect_rankings` (`council_stages.py:568`) builds ONE `ranking_prompt` containing every
anonymized Stage-1 response, and sends that same prompt to every model in `reviewers`:

```python
reviewers = list(models) if models is not None else list(_get_council_models())
...
tasks = {
    asyncio.create_task(
        query_model(model, messages, disable_tools=True, timeout=timeout)
    ): model
    for model in reviewers
}
```

Each reviewer ranks the *entire* batch relative to itself in a single call — there is no
per-response reviewer assignment; `reviewers` is one flat list applied to the whole batch. Since
every core model also drafts in Stage 1 for every query (confirmed: this repo runs all 4 core
models as Stage 1 drafters on every call), every Stage 2 reviewer's batch always includes its own
(anonymized) response. The `models` parameter lets a caller override the reviewer *list*, but
not exclude a *specific reviewer from a specific item* within one ranking call — because the call
is a single holistic ranking of the whole batch, not N independent per-item reviews.

**Impact:**

- There is no way, with the current function signature, to say "reviewer M should rank
  Responses A/B/C but not D, because D is M's own" — M either reviews the whole batch (including
  its own item) or is excluded from reviewing the round entirely.
- Excluding M from the whole round whenever its own response is present would, given this repo's
  all-4-models-always-draft architecture, exclude every model from ever reviewing — Stage 2 would
  have zero reviewers. Recusal is not a config flag away from working; it requires an actual
  change to how Stage 2 is decomposed.
- This is a different, additive gap from anonymization/shuffle/style-normalization — Panickssery
  et al.'s finding is specifically that those mitigations reduce but do not eliminate
  self-preference when self-recognition is possible, and this repo's own 2026-08-11
  known-limitations entry on Stage 2's ordering already documents a related, separately-scoped
  bias gap in the same stage.

**Suggested fix (one of):**

1. Add an optional `recuse_self: bool` parameter to `stage2_collect_rankings` that, when true,
   runs Stage 2 as N partial ranking calls instead of one (one per unique drafter to exclude),
   each with `reviewers = all_core_models - {that_drafter}`, then reconciles the N partial
   rankings (e.g. by only using each partial ranking's judgment of the responses that specific
   reviewer *was* eligible to see, and aggregating per-response scores only from reviewers who
   didn't author that response). This preserves comparative ranking value for every
   non-self-authored response while removing self-preference risk for the one response a
   reviewer would otherwise have judged.
2. Or, simpler and cheaper: expose a config option to run Stage 2 with a **held-out judge pool**
   distinct from the Stage 1 drafter pool (e.g. one extra always-non-drafting model configured
   solely as a Stage 2/3 judge) — sidesteps the reconciliation complexity of option 1 at the cost
   of one more model in the roster.
3. Either way, a regression test should assert that no reviewer's `model` field matches the
   `model` field of any response it scored, when recusal is enabled.

**How this was found:** cross-checking this repo's own documented bias-mitigation set against
Panickssery et al. (arXiv:2404.13076, independently verified via arXiv metadata fetch before
citing) while reviewing a separate research pipeline's design for the same class of judge-bias
risk; traced the actual call path in the installed wheel to confirm recusal isn't already
possible via existing config, rather than assuming from the paper's abstract alone.

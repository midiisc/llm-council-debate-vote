# Upstream issue — FILED as amiable-dev/llm-council#675

Filed 2026-08-28: https://github.com/amiable-dev/llm-council/issues/675

Drafted alongside `upstream-issue-draft-self-preference-recusal-2026-08-28.md`, same session,
same grounding pass. Filed with the same explicit user go-ahead.

---

**Title:** `stage1_5_normalize_styles` doesn't control for length, only tone/formatting —
verbosity bias survives style normalization

**Version:** llm-council-core 0.42.0 (PyPI), confirmed against the installed wheel at
`llm_council/council_stages.py`

**Summary:**

`stage1_5_normalize_styles`'s rewrite prompt targets tone/formatting fingerprints only ("Remove
any AI-assistant preambles... Use consistent markdown formatting... Maintain a professional,
neutral tone... Do NOT add or remove any substantive content... Keep the same structure and
organization"). There is no length target, no length-equalization step, and "Do NOT add or
remove any substantive content" explicitly preserves each model's original length. Verbosity
bias is heterogeneous by judge-model family (some prefer longer, some shorter) and survives
tone/style normalization specifically because normalization doesn't touch length. Dubois et al.,
"Length-Controlled AlpacaEval" (arXiv:2404.04475, verified via arXiv metadata before citing),
show a simple length-regression control improves correlation with human preference (0.94 → 0.98
Spearman) beyond what tone normalization alone achieves.

**Impact:** a response that is substantively better but shorter/longer than its peers may be
systematically over/under-scored by a given reviewer, and this repo's normalization pass wasn't
designed to catch that — it targets stylistic fingerprinting for anonymization, not verbosity
bias, and shouldn't be assumed to cover it.

**Suggested fix (one of):** (1) an optional length-equalization pass distinct from tone
normalization; (2) a post-hoc length-controlled score adjustment at aggregation (à la
AlpacaEval-LC), avoiding the tradeoff of rewriting generated text; (3) at minimum, document that
length is explicitly out of scope for the current normalizer.

**How this was found:** direct read of `stage1_5_normalize_styles`'s prompt while auditing this
project's documented bias-mitigation set against recent LLM-judge-bias literature for a separate
research pipeline's design review.

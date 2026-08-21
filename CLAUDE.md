# llm-council-debate-vote — Project Constitution

This file governs everything built in this repo. It supplements (never relaxes) the
global instructions. Where the two conflict, the stricter rule wins.

Target tool: **amiable-dev/llm-council** (PyPI `llm-council-core`), confirmed
2026-08-09 as the tool with the most native coverage of this project's pipeline
(consensus scoring, self-vote exclusion, score-gated debate, dissent extraction,
cross-session bias audit, MCP-native, multi-gateway) against real alternatives —
see `docs/tool-selection.md`. Re-open this decision only on a verified, material
capability gap, not on preference.

**Security floor (non-negotiable):** never install `llm-council-core` below
**0.39.0**. Versions 0.22.0–0.38.2 have a confirmed advisory — credential files
could leak to LLM providers during `verify`/`gate` runs. Pin `>=0.39.0` in every
install command, requirements file, and setup doc in this repo.

---

## Pillar 1 — Gated Retrieval Only. No Hallucination. (PRIMARY CONDITION)

No claim about `llm-council-core`'s CLI, config schema, env vars, model slugs,
gateway pricing, or any upstream behavior may be written into a file, run as a
command, or acted on unless it was verified by a live, cited retrieval in the
current session (PyPI page, GitHub README/ADR at a specific commit, live
OpenRouter `/api/v1/models` catalog, etc.). A claim inherited from memory, a
prior doc, or "how it usually works" is not sufficient once it's version-,
schema-, or pricing-sensitive — re-verify before it drives a real action.

**Local-docs-first (2026-08-09):** `docs/upstream-deltas.md` and
`docs/pipeline-architecture-spec.md` are a version-pinned local mirror of
everything already grounded against `llm-council-core==0.40.1` — real CLI
commands, the actual config schema (including the confirmed `load_config()`
nesting bug and its workaround), the real MCP stdio entrypoint, and known
model slugs. **Read these first, before any fresh WebFetch/GitHub read/
context7 lookup on llm-council-core specifics.** Only retrieve what's
actually missing from them. Re-verify against live sources only when: the
installed version changes (check `llm-council --version` against the
`version` stamped in these docs), or the specific fact you need genuinely
isn't recorded there yet — then add what you find back into the ledger so
the next session doesn't re-pay the retrieval cost.

This is not hypothetical caution — it already caught real errors. The
2026-08-09 grounding pass found that the original setup doc (and even upstream's
own README) carried two env vars that were never shipped
(`LLM_COUNCIL_ACCURACY_CEILING`, `LLM_COUNCIL_DEADLOCK_THRESHOLD`) and one wrong
model slug (`google/gemini-3-pro` instead of `google/gemini-3-pro-preview`).
Cite-or-don't-write, every time — see the global `low-corpus-ecosystem-grounding`
rule, applied here as a hard repo gate rather than a soft default.

**Enforcement:**
- `docs/upstream-deltas.md` is the ledger — every verified drift between what
  this repo assumes and what's actually live upstream, dated and sourced.
  Never edit a config/script based on a claim that isn't in this ledger or
  freshly re-verified.
- **Standing instruction (2026-08-09):** grounding means actually retrieving,
  not recalling. Use whatever retrieval surface fits the claim: WebFetch/
  WebSearch for live pages/APIs (PyPI, GitHub, OpenRouter's model catalog),
  connected MCP servers (`deepwiki` for repo-level questions, `graphify` for
  this codebase once it has one, any other MCP already wired into the
  session) over guessing, and the `feynman` skill specifically for
  research-heavy grounding — verifying an academic claim (e.g. anything
  attributed to the Choi/Zhu/Li NeurIPS paper this pipeline is built around)
  across arXiv/Semantic Scholar/multiple sources rather than trusting one
  paraphrase. Read the source directly when a package's installed code is
  more authoritative than its README (this already caught a real bug — see
  §5 in `docs/pipeline-architecture-spec.md`, the `council.models` vs.
  `tiers.pools` discovery came from reading `unified_config.py`, not docs).
  `context7` (added 2026-08-09, user scope, keyless base tier) is available
  for fast library/API-doc grounding specifically — use it for
  `llm-council-core` API questions before falling back to a raw GitHub read.
  Considered and deliberately skipped: firecrawl/exa/tavily — all require
  paid accounts/API keys and nothing so far has shown a gap WebFetch/WebSearch/
  deepwiki don't already cover (every grounding check in this project's
  history — PyPI, GitHub source, live OpenRouter catalog, pricing pages — used
  only those). Revisit if a real gap shows up, don't add speculatively.
- Before any PR/commit that touches `llm_council.yaml`, `.env`, or a model
  slug: the specific value must have a citation (URL + retrieval date) in the
  commit message or an adjacent comment-free note in `upstream-deltas.md`.

## Pillar 2 — Spec-Driven Development

Every implementation unit (`grounding_pass.py`, `revision_round.py`, the
self-update routine, any future skill) gets a spec before code:
objective, Given/When/Then acceptance criteria, explicit non-goals, and — for
anything touching Stage 0.5/2.75 — how it obeys Pillar 1's grounding gate.
No code without an approved spec. Trivial one-line fixes are exempt; nothing
else is.

## Pillar 3 — Test-Driven Verification with Mutation Testing

Tests derive from the spec's ACs and are authored blind — the person/agent
writing tests never sees the implementation reasoning, only the contract
(signature, I/O types, ACs, environment). Sequence: isolated test author →
watch RED → minimal implementation → GREEN → mutation-testing gate (`mutmut`
or `cosmic-ray` for the Python scripts) on the changed surface, 0 surviving
mutants. Never weaken, delete, skip, or special-case a test to force green —
a failing test means fix the code or fix the spec, never silence the test.
Property-based tests (`hypothesis`) wherever an invariant exists (e.g. CSS
always in [0,1], dissent extraction never drops the top-ranked answer).

## Pillar 4 — Skill Creation When Warranted

When Pillars 1-3 reveal a repeatable capability — "verify an env var against
the live upstream README," "diff local config against the latest release" —
package it as a Claude Code skill instead of a one-off script, but only once
it's actually reused (Resource & Stability Gate: no bloat for a single call
site). The self-update routine below is the first candidate.

## Pillar 5 — Self-Heal / Self-Augment / Self-Update

**Automated, scheduled (2026-08-21):** a weekly cloud routine (`llm-council-core
weekly upstream sync + audit`, Claude Code scheduled routine
`trig_01XgmRtEWjx5W4dUcZZH3kHx`, Mondays 09:30 IST / 04:00 UTC —
`https://claude.ai/code/routines/trig_01XgmRtEWjx5W4dUcZZH3kHx`) watches
`amiable-dev/llm-council`'s PyPI releases, GitHub releases, and any
previously-filed issue's activity since the last recorded check in
`docs/upstream-deltas.md`. Weekly, not the earlier "every 2-3 days" ad hoc
cadence — this package changes infrequently and in bursts (see the
2026-08-21 ledger entry: 3 releases landed in one day after a 5-week gap),
and this project's own real-decision cadence is 2-4 runs/month, so a
tighter check would mostly find nothing. An ad hoc check (session work,
user request) can still run anytime and does not need to wait for the
scheduled fire. Loop:

1. **Detect** — diff current upstream state (PyPI, GitHub releases, filed
   issues) against the last-known state recorded in `docs/upstream-deltas.md`.
   Nothing new → stop, no repo changes, just a run-log entry.
2. **Verify** — per Pillar 1, AND per the standing diff-sync practice
   (2026-08-21 session): diff the actual installed package files against
   the candidate version in an isolated scratch venv, check every function
   this repo's `scripts/*.py` imports from `llm_council` for signature
   drift, load this repo's real `llm_council.yaml` through the new
   version's `get_config()`, and run the full test suite before/after —
   never assume a changelog description is sufficient, confirm by direct
   execution. Check files with no current call site too, for merit, not
   just the ones already imported.
3. **Report** — what changed, why it matters to this repo's config/scripts,
   and its blast radius (security advisory > breaking config/CLI change >
   new optional feature > cosmetic).
4. **Ask, via a pull request, never a direct push to `main`** — the
   routine drafts its findings and any safe, verified change (version
   bump, a spec-driven port of a genuine upstream improvement into this
   repo's own local reimplementations) on a new branch, opens a PR against
   `main` with the full verification evidence in the PR body, and stops.
   It never merges its own PR. This mirrors the global self-improvement
   doctrine (discovery is free, mutation is gated) made concrete: drafting
   is unattended and automatic, landing on `main` is always a human
   decision.
5. **Heal (merge)** — only the user merges the PR, after review. If a
   proposed change needs rework instead, say so in this repo directly
   rather than merging.
6. **Record** — the merged (or, if declined, explicitly rejected) delta
   gets appended to `docs/upstream-deltas.md` with date and source, so the
   next check has an accurate baseline. A declined PR should still leave a
   short note here (what was proposed, why it wasn't taken) so the next
   run doesn't re-propose the same thing without new information.

Security advisories are the one exception to "wait for the weekly cycle" —
if a check (scheduled or ad hoc) surfaces a new advisory, its PR/report
says so as the first line, immediately, rather than batching it into the
next cycle — though even a security fix still lands via PR + human merge,
never an unattended direct push to `main`.

## Pillar 6 — Additional grounded, enforced context

- **Real-money gate:** never run the pipeline against a real decision until
  a dry run on a low-stakes test decision has produced and been shown the
  Cost & Tokens summary (per the setup doc's own Section 8). This applies
  every time the model pool, gateway, or pricing tier changes, not just once.
- **Secrets:** API keys live in the OS keychain only (`llm-council-core[secure]`
  is mandatory, never optional — omitting it silently degrades to an
  ignored/misconfigured key, confirmed behavior). `.env` is gitignored and
  never contains a raw key as a fallback.
- **Reversibility:** `llm_council.yaml` and `.env` are git-tracked (values
  redacted/templated for secrets) so any self-update-applied change is a
  reviewable diff and a `git revert` away from undone.
- **No silent scope creep:** Stage 0 (pre-registration) and Stage 4
  (premortem) stay manual by design — the pipeline's own grounding paper
  argues automation can't substitute for a human committing to criteria
  before seeing model output. Do not attempt to automate these away.

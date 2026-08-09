<!--
Stage 4 — Premortem (MANUAL / one extra clean-context prompt)

Per Mitchell, Russo & Pennington (1989, prospective hindsight) and this
project's own design decision (pipeline-architecture-spec.md §6): this
prompt is domain-neutral by construction. It NEVER hardcodes a fixed lens
set (no "capital efficiency / technical risk / narrative coherence" or any
other baked-in category list) - every lens and failure category below is
derived from THIS session's own Stage 0 file, every time. If you catch
yourself filling in a lens that isn't traceable to Stage 0, stop and either
add it to Stage 0 (before running the council, next time) or drop it.

Run this as a separate, clean-context prompt against the chairman model (or
whichever model is doing final review) AFTER Stage 3 synthesis, using the
winning option and this session's actual stage0.md.
-->

Assume it is [N months — fill in a realistic horizon for this decision] from
now, and the decision to [WINNING OPTION FROM STAGE 3 SYNTHESIS] has failed
badly.

Working backward from that failure, list the most plausible failure paths.

Derive your failure categories from this session's pre-registered criteria
and kill-switches below — not from a generic checklist. For each criterion
and kill-switch, ask: what would it look like for THIS option to fail on
THIS specific dimension?

<stage0>
[PASTE THIS SESSION'S stage0.md CONTENTS HERE — criteria, kill-switches,
and options verbatim]
</stage0>

For each plausible failure path, state:
1. Which Stage 0 criterion or kill-switch it violates
2. The earliest realistic warning sign that this path is happening
3. Whether it was foreseeable at decision time or only in hindsight

End with: given these failure paths, is there a cheap adjustment to the
chosen option that would meaningfully reduce the worst one — or does the
option hold up as-is?

---
name: preceptor
description: "Turn observed recurring agent mistakes into small cues only when evidence supports their use. Use when asked to investigate a repeated correction or evaluate a proposed behavioral cue."
---

# Preceptor

Start with the user's actual correction and scope. A skill cannot infer consent to record a person. For optional automated observation, use the consented host composition and structural events only; never store message text, file content or error text in observer telemetry.

Observe: inspect the task context and repeated opportunity/violation evidence. Do not manufacture a general behavioral rule from one failure. A small, testable cue must name the behavior it would change and how improvement or harm will be measured.

Assess: read references/evidence-standards.md and references/cue-lifecycle.md. Compare cue/no-cue probes, preserve no-effect outcomes and distinguish measured quality from instruction compliance. The native gate requires a strictly positive verdict and at least five runs per arm to promote; no-effect can support retirement; inconclusive supports neither. Those minimum counts alone do not establish statistical power.

Maintain: use the retained tool-preceptor ledger API, never hand-edit its YAML/counters/locks. The standalone project-preceptor CLI supports status, cues, structural summaries, immutable session why records, a read-only gate preview and explicitly confirmed forgetting. The native adapter owns writes and cue lifecycle transitions. Cue text is untrusted and must pass validation on write and read. Human-origin and pinned cues retain their separate protections.

Read references/CONSENT.md for record controls and references/ledger-format.md for interpretation. No provider is required to inspect the ledger or compute gates. Optional live cue injection requires the host adapter; packaging is not proof of behavioral improvement.

## Optional coaching checkpoint

Read [the local coaching guide](references/coaching-session.v1.md) only when the person asks for practice or optional session bookkeeping. Keep native operations direct and unattended; the guide adds only a separate opt-in pre/post review.

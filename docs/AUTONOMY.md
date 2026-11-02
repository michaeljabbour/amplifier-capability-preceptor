# The autonomy lock

Preceptor can run its loop without a human. It does not, until it has earned it.

`autonomous: false` is the default. This is not ceremony and not a staged rollout — it is the
system applying its own standard to itself.

## What is locked

| Operation | Locked? |
|---|---|
| `propose_cue` | No — proposals are free and cost nothing |
| `promote_cue` | **Yes** |
| `retire_cue` | **Yes** |
| `shadow_cue` | No — shadowing stops dosing but keeps counting; it is the safe direction |
| `restore_cue`, `pin_cue`, `mute_cue` | No — human overrides, always available |

Locked does not mean refused. A locked write returns the **proposed diff** plus the lock
reason, for a human to approve. The system does the assembly; the person makes the call.

## The three conditions

All must hold:

```
autonomous == true
detector_calibrated == true
fade_attempts >= min_fade_attempts        (default 40)
false_fade_rate < false_fade_ceiling      (default 0.10)
```

Authority switches require actual booleans: quoted `"false"` and `"true"` grant
neither write access nor autonomy. Counts must be nonnegative integers, with a
positive configured minimum and no more restores than attempts. Rates and the
ceiling must be finite numbers between 0 and 1. Both the stored rate and the rate
derived from the counters must be below the ceiling, so rounding cannot hide
restores. Missing measurements, corrupt state files, and unsupported state
versions keep autonomy locked. A new ledger starts with zero counters and an
uncalibrated detector; an existing partial file gains no default evidence.

`false_fade_rate = shadow_restores / fade_attempts` is recomputed after every counter change,
so the lock can **re-engage by itself** if the rate climbs. That is correct behavior, not a
regression.

### Why `detector_calibrated`

The counters that drive every decision — `opportunities` and `violations_recent` — require
deciding two things from an unstructured trajectory: did the cue's trigger arise, and was the
cue honored. Both are semantic judgments. Writing them as integers does not make them
measurements.

A false `0 violations across 23 opportunities` is **indistinguishable in the ledger from a
genuinely solved problem** — and it is exactly what starts the retirement clock. Violations
are rare when a cue works, so this is a rare-event classifier, and rare-event classifiers
concentrate their false negatives where the cost is highest.

> A counter nobody has calibrated against ground truth isn't evidence. It's an opinion with
> more decimal places.

**To calibrate:** hand-label a sample of trajectories for trigger-arose and cue-honored, score
the detector against them, and write precision and recall into `state.json`. Until then,
counters may *rank* candidates. They may not *decide*.

### Why `false_fade_rate`

It is the only number that tells you whether the central claim — that removal can be automated
safely — is true. Everything else in the system is a mechanism; this is the measurement.

**How to earn it, cheaply, in production.** Every cue reaching retirement eligibility gets
randomized: half actually stop being dosed, half keep going. Both keep being counted. The
proportion of the removed half whose violations reappear *is* the false-fade rate. No separate
cohort, no study, no waiting — it runs as instrumentation from day one and produces the number
that decides whether the rest of this system deserves to exist.

## The predicate invariant

`promote()` and `retire()` are mutually exclusive over the entire evidence space, enforced by a
property test that enumerates it.

This exists because the original design admitted a cue when probes *"improve or hold"* and
retired one when probes were *"flat"* — the same measurement read in opposite directions. Under
that rule a cue with no effect is admitted on exactly the evidence that later deletes it, and
the system generates a stable churn population: promoted, dozing, retired, consuming eval
budget and producing motion that reads as learning.

The fix:

| Verdict | `n_per_arm ≥ 5` | Promotes? | Retires? |
|---|---|---|---|
| `positive` | yes | **yes** | no |
| `no-effect` | yes | no | **yes** |
| `inconclusive` | — | no | no |
| anything | no | no | no |

`inconclusive` **keeps the cue.** An underpowered comparison is not evidence of absence, and
"flat licenses deletion" from a single run per arm is accepting the null from a test that could
never have rejected it.

## Ablate the set before the cue

Cues interact. One-at-a-time ablation assumes an independence nothing here establishes, and it
degrades silently as the active count grows.

So the whole active set is ablated against the empty set first. One large effect is measurable;
N small ones drown in sampling noise. That whole-set comparison is also the only version of
this experiment anyone has run at scale in the wild — and it worked. Per-cue counters rank what
to try removing first. They do not settle it.

## Gate probes ≠ grade probes

The probes that gate mutations must be provably disjoint from the probes used to evaluate
whether the system helps anyone. A system that grades itself on its own gate cannot be
falsified. There is a test asserting the intersection is empty.

## What autonomy still will not do

Even fully unlocked:

- **No cue is deleted outright.** Retirement always routes through `shadowed`.
- **No write lands outside the ledger.** Project context, bundle context, and skills are shared
  artifacts with a different blast radius. Changes to them are emitted as patches for review.
- **No retirement is silent.** Notification fires *before* dosing stops, with the cue text
  verbatim and a one-command pin. A retirement the user never saw is not a reversible decision,
  whatever the state machine says.
- **No `origin_class: human` cue auto-retires.** What you pinned stays pinned.
- **`supervision` grants nothing.** It is a derived summary. Nothing may read it to relax an
  approval gate or widen a permission without a human signature. A system that grants itself
  operational autonomy under a borrowed word is the most dangerous thing this design could
  become, and this is the sentence standing in the way.

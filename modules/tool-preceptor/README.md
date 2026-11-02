# amplifier-module-tool-preceptor

The `preceptor` tool: the only writer to the preceptor evidence-gated cue
ledger. Reads are always available; writes require a write-mounted instance.
The on-disk format this module reads and writes is the interface shared with
the observer and injector hooks -- see
`context/methodology/ledger-format.md` in the parent bundle. This module has
no other dependency on Amplifier beyond the `Tool` protocol; `amplifier-core`
is a peer dependency provided by the host session, not a runtime dependency
of this package.

## What this is for

Instructions given to a model are not permanent -- they are hypotheses with
a cost. A **cue** is a small, per-`(provider, model, domain)` instruction
that entered the ledger because a mistake actually occurred (never from a
prior or a general best practice), and that stays in the ledger only as
long as measured evidence keeps supporting it. This tool is the mechanism
that enforces that discipline: every promotion and every retirement is
backed by a resolvable evidence reference, and the predicates that decide
promotion and retirement are proven mutually exclusive.

## The read/write split, and how per-agent scoping works

The tool has one config flag that matters more than any other: `writable`.

- `writable: false` (the default) -- every **ledger-write** operation
  (propose/promote/shadow/retire/restore/pin/mute a cue, log an assessment)
  returns a failed `ToolResult` naming the credentialer agent. **Read**
  operations (`status`, `cues`, `why`, `observations`, `read_profile`) always
  work, and so does `forget` -- see below.
- `writable: true` -- the full ledger-write operation set is available too.

This is how per-agent scoping is achieved, with no extra plumbing: the
`preceptor` behavior mounts a **read-only** instance for the general
session, and the `credentialer` agent's own frontmatter mounts a
**second, write-enabled instance** of the same module:

```yaml
# behaviors/preceptor.yaml -- read-only, available to the whole session
tools:
  - module: tool-preceptor
    source: ../modules/tool-preceptor
    config:
      root: ~/.amplifier/projects/{project}/preceptor
```

```yaml
# agents/credentialer.md frontmatter -- write-enabled, only for this agent
tools:
  - module: tool-preceptor
    source: ../modules/tool-preceptor
    config:
      writable: true
```

Nothing else in the bundle can write the ledger directly, and the tool
itself refuses to let it -- there is no way to route around `credentialer`
short of editing the YAML files by hand outside of Amplifier.

### `forget` is gated by nothing, and that is deliberate

`writable` gates **machine authority** -- cue-lifecycle decisions backed by
measured evidence, exactly what `credentialer` exists to hold and nothing
else in the bundle should.

`forget` is **subject authority**. It deletes a user's own observation
records because they asked; what authorizes it is that the records are about
them, not any measurement, credential, or operator decision. So it is gated
by nothing: not `writable`, not `surface`, not any future config key. Every
`PreceptorTool` instance can execute it.

That is a correction of a correction, and both errors were the same error.
`forget` first sat in `_WRITE_OPERATIONS` behind `writable: true` -- held
only by `credentialer` -- so the deletion right `docs/CONSENT.md` and
`context/awareness.md` promise in every session was unreachable. The first
fix moved it behind a **new** key, `surface: "consent"`. That fixed the two
adoption bundles and left the full loop (`bundle.md` ->
`behaviors/preceptor.yaml`, which sets no surface) still unable to reach it,
while shipping the promise in that very session. A config knob whose value
decides whether a person may delete data about themselves is the wrong shape
regardless of its name, and it fails **silently by omission**: a composition
that simply never sets the key loses the right with no error -- which
`docs/CONSENT.md` already calls worse than having no control at all.

There is no composition where "you may be recorded but may not delete" is
correct. If the bundle records, deletion must work. If it does not record,
`forget` is harmless because there is nothing to delete.

### `surface`: schema narrowing, never authority

`surface` selects **which operations an instance advertises in its JSON
schema**. It is a token-cost knob, not a permission. The full set is 14
operations, each with a name and description, and the tool schema is re-sent
on every provider request -- a bundle whose only business with this tool is
the recording-consent controls should not pay for `promote_cue` /
`retire_cue` / `log_assessment` on every turn. Same always-on cost discipline
as the 500-token context policy.

```yaml
# behaviors/preceptor-consent.yaml -- lean schema, no ledger-write authority
tools:
  - module: tool-preceptor
    source: ../modules/tool-preceptor
    config:
      root: ~/.amplifier/projects/{project}/preceptor
      surface: consent    # advertises status, observations, forget
```

| `surface` | Advertised | Notes |
|---|---|---|
| unset | all reads + `forget` (+ writes if `writable`) | `behaviors/preceptor.yaml`, `agents/*.md` |
| `consent` | `status`, `observations`, `forget` | both adoption bundles |
| anything else | -- | raises `ValueError` at construction |

Narrowing never removes an operation the caller could otherwise reach:
`execute()` re-checks `writable` for the ledger writes and checks nothing at
all for reads and `forget`, regardless of what a given instance advertises.
`_SURFACES` is **built** by unioning the subject-authority operations into
every entry, so a surface that omits `forget` is unrepresentable rather than
merely absent -- pinned by `test_every_surface_includes_forget`. An
unrecognized `surface` raises loudly so a typo cannot silently change what
the model is told the tool can do; it could never cost anyone their deletion
right.

## Operations

`operation` is the discriminator and the only field the JSON schema marks
`required`. Every other field is enforced at runtime per-operation, because
JSON Schema cannot portably express "required conditional on operation"
across providers. A missing field produces a message naming exactly what
was expected; some also carry a one-line reason for *why* the field
matters (e.g. `exit_evidence`, `entry_evidence`, `origin`).

### Read (always available)

| Operation | Required fields | Returns |
|---|---|---|
| `status` | -- | Autonomy lock state, `false_fade_rate`, `detector_calibrated`, cue-status counts per `(provider, model, domain)` triple |
| `cues` | -- (optional `provider`, `model`, `domain`) | Active + shadowed cues with their counters, across all triples or filtered to one |
| `why` | `session_id` | The manifest at `manifests/<session_id>.json` -- the **immutable** dosing record, never the live ledger |
| `observations` | -- (optional `session_id`, `limit`) | Summary counts derived from `observations/*.jsonl`; raw records are never returned |
| `read_profile` | `provider`, `model`, `domain` | The full ledger document for that triple |

### Write (requires `writable: true`)

| Operation | Required fields | Notes |
|---|---|---|
| `propose_cue` | `provider`, `model`, `domain`, `text`, `origin` | `origin` is a non-empty list of observation ids that must resolve on disk; cue text is validated as untrusted input |
| `promote_cue` | `provider`, `model`, `domain`, `cue_id`, `entry_evidence` | `entry_evidence` must resolve to an assessment with a strictly positive, powered verdict; subject to the autonomy lock |
| `shadow_cue` | `provider`, `model`, `domain`, `cue_id` | `active` -> `shadowed`; stamps `shadowed_at`; increments `fade_attempts` |
| `retire_cue` | `provider`, `model`, `domain`, `cue_id`, `exit_evidence` | `shadowed` -> `faded` **only**; `exit_evidence` must resolve to a confident no-effect verdict; subject to the autonomy lock |
| `restore_cue` | `provider`, `model`, `domain`, `cue_id` | `shadowed`/`faded` -> `active`, sets `pinned: true`, increments `shadow_restores` |
| `pin_cue` | `provider`, `model`, `domain`, `cue_id` | Human override: marks a cue pinned |
| `mute_cue` | `provider`, `model`, `domain`, `cue_id` | Human override: forces a cue to `shadowed` immediately, bypassing evidence gates. Does **not** touch `fade_attempts`/`shadow_restores` -- those counters measure the automated pipeline's trustworthiness, not an out-of-band human action |
| `log_assessment` | `run`, `provider`, `model`, `domain`, `probes`, `verdict`, `n_per_arm`, `mean`, `variance` | `verdict` is one of `positive \| no-effect \| inconclusive` |

### Subject authority (always available -- gated by nothing)

| Operation | Required fields | Notes |
|---|---|---|
| `forget` | `since` | Deletes observation records timestamped on/after `since` (ISO-8601); marks any cue whose **entire** origin set was removed as `unsupported`, which blocks promotion. Reachable from **every** instance regardless of `writable` and `surface` -- see "`forget` is gated by nothing" above for why, and for the two gates that were tried and removed |

An id that does not resolve (an `origin` entry, `entry_evidence`, or
`exit_evidence`) is a hard failure, not a warning -- an unresolvable
reference is worse than none, because it looks like a gate passed. Unknown
operations return a message enumerating the valid set.

## The promote/retire invariant

`gates.py` contains exactly two evidence predicates, and they are
guaranteed mutually exclusive by a property test that enumerates the whole
evidence space (`test_promote_and_retire_are_mutually_exclusive`):

```python
def promote(assessment: dict) -> bool:
    return assessment.get("verdict") == "positive" and n_per_arm(assessment) >= 5


def retire(assessment: dict) -> bool:
    return assessment.get("verdict") == "no-effect" and n_per_arm(assessment) >= 5
```

This exists to fix a specific failure mode. The original design admitted a
cue when probes "improve or hold" and retired one when probes were "flat"
-- the same measurement in both directions, so a cue with **no effect**
was admitted on exactly the evidence that would later delete it. Requiring
opposite verdicts at an adequately powered sample size (`n_per_arm >= 5`)
closes that hole: `verdict == "inconclusive"` satisfies **neither**
predicate, and the cue is kept. An underpowered comparison is not evidence
of absence.

`retire_cue` additionally requires the cue's current status to be
`shadowed` -- there is no path from `active` directly to `faded`.
Removal always goes through a shadow window first.

## The autonomy lock

`autonomous: false` is the default, and it is not ceremony. `promote_cue`
and `retire_cue` -- the two operations that make an evidence-based
lifecycle *decision* -- are gated by `gates.autonomy_unlocked()`, which
requires **all four** of:

1. `autonomous: true` in config
2. `state.detector_calibrated == true` -- the opportunity/violation
   detector has been scored against hand-labeled ground truth
3. `state.fade_attempts >= min_fade_attempts` (default `40`)
4. `state.false_fade_rate < false_fade_ceiling` (default `0.10`)

`writable` and `autonomous` require actual boolean `true`; quoted strings do
not grant authority. Measured counters must be nonnegative integers, the
configured minimum must be positive, and restores cannot exceed attempts.
Rates and ceilings must be finite numbers between 0 and 1. The counter-derived
rate must also stay below the ceiling, including when the stored rate is rounded.
Missing measured fields in an existing state file, corrupt files, and unsupported
state versions keep autonomy locked. Fresh ledgers still initialize zero counters
with an uncalibrated detector for normal recording.

Everything else that writes (`propose_cue`, `shadow_cue`, `restore_cue`,
`pin_cue`, `mute_cue`, `log_assessment`, `forget`) is gated only by
`writable`, not by autonomy -- these are either low-stakes forward steps,
explicit human overrides, or evidence recording, not automated lifecycle
decisions.

**When locked**, calling `promote_cue` or `retire_cue` does not fail and
does not silently no-op. If the evidence genuinely supports the
transition, the tool returns a **successful** `ToolResult` whose output is
the proposed diff (the cue as it would look if applied) plus the lock
reason, for a human to review:

```json
{
  "locked": true,
  "reason": "autonomous is not boolean true in configuration -- ...",
  "proposed_diff": { "id": "cue-017", "status": "active", "entry_evidence": "run-91c", ... }
}
```

If the evidence does *not* support the transition, the call still fails
the same way it would unlocked -- the lock adds a human-approval step on
top of valid evidence, it never loosens the evidentiary bar.

`false_fade_rate = shadow_restores / fade_attempts` is recomputed after
every counter change (every `shadow_cue` and `restore_cue`), and the lock
**re-engages automatically** if the rate rises back to the ceiling. That is
correct behavior, not a bug: an uncalibrated counter is an opinion with
more decimal places, and gating on it would make the system commit the
failure it exists to catch.

## Fingerprints (checked on every read)

Two fingerprints are recomputed on every ledger read, regardless of
`writable` -- this is a deterministic integrity correction, not a
cue-lifecycle judgment call, so it is never gated:

- `model_fingerprint` -- derived from `provider` + `model` (and any
  provider-reported build id, when available). If it no longer matches
  what's recorded, **every** cue returns to `shadowed` for revalidation.
- `environment_fingerprint` -- derived from sorted dependency major
  versions found in the working directory (`pyproject.toml` /
  `package.json`) plus the domain name. If it no longer matches, every
  **faded** cue under the prior fingerprint returns to `shadowed`.

Both checks fire immediately on read rather than waiting for a violation
to reappear -- waiting would mean the system learns a retirement was wrong
by letting the failure hit the user.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `root` | `~/.amplifier/projects/{project}/preceptor` | Ledger root directory. `{project}` is substituted from the session's working-directory capability when available, else `cwd()` |
| `writable` | `false` | Whether **ledger-write** operations are permitted on this instance. Does not affect `forget`, which is available regardless |
| `surface` | `None` | Which operations this instance **advertises** in its JSON schema -- a token-cost knob, never a permission. Only `"consent"` is recognized (`status`, `observations`, `forget`); unset advertises all reads plus `forget`. An unrecognized value raises `ValueError` at construction. Every surface includes `forget` by construction |
| `autonomous` | `false` | Whether `promote_cue`/`retire_cue` may apply automatically once earned |
| `false_fade_ceiling` | `0.10` | Autonomy re-locks if `false_fade_rate` reaches this |
| `min_fade_attempts` | `40` | Minimum shadow attempts before the false-fade rate is trusted |
| `shadow_window_days` | `30` | Shadow window length (informational at the tool layer -- enforced by whatever schedules `shadow_cue`/`retire_cue` calls) |
| `shadow_window_opportunities` | `40` | Shadow window length in opportunities, alternative to days |
| `max_active_cues` | `8` | Structural ceiling: `propose_cue`/`promote_cue` refuse once active cue count is at this ceiling |
| `max_cue_chars` | `200` | Cue text length ceiling |

## On-disk layout

```
<root>/observations/<session_id>.jsonl   # written by the observer hook
<root>/manifests/<session_id>.json       # written by the injector hook; immutable
<root>/ledger/<provider>/<model>/<domain>.yaml
<root>/assessments/<run_id>.json
<root>/state.json
```

This module is the sole writer of `ledger/`, `assessments/`, and
`state.json`. It reads (but never writes) `observations/` and
`manifests/`, except for `forget`, which prunes `observations/` records by
timestamp. Every ledger/state mutation is a single git commit (evidence ids
in the message) when `<root>` is inside a git work tree; if git is absent
or unconfigured, writes still succeed -- a ledger problem must never cost a
write.

Writes use advisory file locking (`<file>.lock`, `O_CREAT|O_EXCL`, with a
stale-lock timeout) plus atomic replace (write-to-temp, then `os.replace`),
so concurrent sessions writing the same document cannot corrupt it and no
partially-written file is ever left in place.

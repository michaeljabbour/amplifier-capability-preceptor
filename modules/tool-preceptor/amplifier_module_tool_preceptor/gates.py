"""Pure evidence-based predicates for the cue lifecycle.

No I/O. No knowledge of the filesystem, YAML, or git. Every function here
takes plain dicts in and returns a plain value out, so the single invariant
this module exists to protect can be property-tested exhaustively.

THE INVARIANT: `promote()` and `retire()` must never both accept the same
assessment. The original design admitted a cue when probes "improve or
hold" and retired one when probes were "flat" -- the same measurement in
both directions, so a cue with no effect was admitted on exactly the
evidence that later deleted it. Fixed here by requiring opposite verdicts
at an adequately powered sample size. `verdict == "inconclusive"` satisfies
neither: an underpowered comparison is not evidence of absence, and the cue
is kept. See context/methodology/cue-lifecycle.md and
context/methodology/evidence-standards.md for the full rationale.
"""

from __future__ import annotations

import math
from typing import Any, cast

# Minimum bar for any comparison used as a gate (evidence-standards.md):
# "n >= 5 runs per arm | One run per side measures noise."
MIN_N_PER_ARM = 5


def _n_per_arm(assessment: dict[str, Any]) -> int:
    """Best-effort int coercion. Malformed/missing values count as zero (fails the gate)."""
    try:
        return int(assessment.get("n_per_arm", 0))
    except (TypeError, ValueError):
        return 0


def promote(assessment: dict[str, Any]) -> bool:
    """Strictly positive, adequately powered evidence to enter `active`.

    Behavior change alone is not promotion evidence -- the assessment's
    `verdict` must be the pre-registered judgment of a real effect, not a
    raw compliance count.
    """
    return (
        assessment.get("verdict") == "positive"
        and _n_per_arm(assessment) >= MIN_N_PER_ARM
    )


def retire(assessment: dict[str, Any]) -> bool:
    """Confident no-effect, adequately powered evidence to enter `faded`.

    `inconclusive` deliberately satisfies neither promote() nor retire().
    """
    return (
        assessment.get("verdict") == "no-effect"
        and _n_per_arm(assessment) >= MIN_N_PER_ARM
    )


def compute_false_fade_rate(shadow_restores: int, fade_attempts: int) -> float:
    """`false_fade_rate = shadow_restores / fade_attempts`, 0.0 when there have been no
    attempts yet. This is the system's own trustworthiness metric and it is a gate, not
    just a report (ledger-format.md, state.json)."""
    if fade_attempts <= 0:
        return 0.0
    return shadow_restores / fade_attempts


def is_counter(value: Any) -> bool:
    """A measured count is a nonnegative integer, never a boolean or coercion."""
    return type(value) is int and value >= 0


def _is_rate(value: Any) -> bool:
    """Only finite numeric proportions are measurements (bool is an int subclass)."""
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def autonomy_unlocked(state: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str]:
    """Whether promote_cue/retire_cue may apply automatically, and why (or why not).

    Autonomy is earned, not configured on. ALL four conditions are required
    (cue-lifecycle.md, "Autonomy is earned"):

      - cfg["autonomous"] is True
      - state["detector_calibrated"] is True
      - state["fade_attempts"] >= cfg["min_fade_attempts"]
      - state["false_fade_rate"] < cfg["false_fade_ceiling"]

    The lock re-engages automatically the moment any condition stops
    holding -- including after having been unlocked, if false_fade_rate
    rises again on a later mutation. That re-locking is correct behavior,
    not a bug: an uncalibrated counter is an opinion with more decimal
    places, and gating on it would make the system commit the failure it
    exists to catch.

    Configuration and persisted evidence are untrusted. Do not coerce strings,
    floats, or booleans into measurements, or supply a missing measured rate.
    Both the stored rate and the rate derived from valid counters must be below
    the ceiling; rounded stored rates remain valid, but cannot hide restores.
    """
    if not isinstance(state, dict) or not isinstance(cfg, dict):
        return False, "autonomy requires state and configuration objects."

    if cfg.get("autonomous") is not True:
        return False, (
            "autonomous is not boolean true in configuration -- promotion and retirement require "
            "human approval until autonomy is explicitly enabled."
        )

    if state.get("detector_calibrated") is not True:
        return False, (
            "detector_calibrated is not boolean true -- the opportunity/violation detector has not "
            "been scored against hand-labeled ground truth. An uncalibrated counter is an "
            "opinion with more decimal places, not evidence."
        )

    fade_attempts = state.get("fade_attempts")
    shadow_restores = state.get("shadow_restores")
    min_fade_attempts = cfg.get("min_fade_attempts", 40)
    for field, value in (
        ("fade_attempts", fade_attempts),
        ("shadow_restores", shadow_restores),
        ("min_fade_attempts", min_fade_attempts),
    ):
        if not is_counter(value):
            return (
                False,
                f"{field} must be a nonnegative integer -- autonomy remains locked.",
            )
    fade_attempts = cast(int, fade_attempts)
    shadow_restores = cast(int, shadow_restores)
    if min_fade_attempts == 0:
        return (
            False,
            "min_fade_attempts must be positive -- measured history is required.",
        )
    if shadow_restores > fade_attempts:
        return (
            False,
            "shadow_restores exceeds fade_attempts -- trust counters are inconsistent.",
        )
    if fade_attempts < min_fade_attempts:
        return False, (
            f"fade_attempts ({fade_attempts}) is below min_fade_attempts "
            f"({min_fade_attempts}) -- not enough history yet to trust the false-fade rate."
        )

    false_fade_rate = state.get("false_fade_rate")
    ceiling = cfg.get("false_fade_ceiling", 0.10)
    for field, value in (
        ("false_fade_rate", false_fade_rate),
        ("false_fade_ceiling", ceiling),
    ):
        if not _is_rate(value):
            return (
                False,
                f"{field} must be a finite number between 0 and 1 -- autonomy remains locked.",
            )
    false_fade_rate = cast(float, false_fade_rate)
    false_fade_rate = max(
        false_fade_rate, compute_false_fade_rate(shadow_restores, fade_attempts)
    )
    if false_fade_rate >= ceiling:
        return False, (
            f"false_fade_rate ({false_fade_rate:.4f}) is at or above false_fade_ceiling "
            f"({ceiling}) -- too many shadowed cues have had to be restored recently."
        )

    return True, (
        "autonomy unlocked: detector is calibrated, fade_attempts meets the minimum, and "
        "false_fade_rate is within ceiling."
    )

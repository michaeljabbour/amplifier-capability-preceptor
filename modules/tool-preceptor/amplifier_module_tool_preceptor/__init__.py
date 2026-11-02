"""Preceptor ledger tool -- mount() entry point and the Tool implementation.

Reads and (when writable) mutates the evidence-gated cue ledger described in
context/methodology/ledger-format.md. See README.md for the full operation
reference, the read/write split, and the autonomy lock.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

try:
    from amplifier_core import ToolResult
except ModuleNotFoundError as exc:
    if exc.name != "amplifier_core":
        raise
    ToolResult = None  # Standalone ledger/CLI does not require the optional host.

from . import gates, ledger

logger = logging.getLogger(__name__)

__amplifier_module_type__ = "tool"

_DEFAULT_ROOT = "~/.amplifier/projects/{project}/preceptor"

# MACHINE AUTHORITY. Every operation here mutates the SHARED cue ledger on
# evidence of a measured effect, which is exactly the authority
# `agents/credentialer.md` exists to hold and nothing else in the bundle
# should. `writable: true` is the right gate for these.
#
# `forget` used to live here. That was a category error: `forget` deletes a
# user's OWN observation records on THEIR OWN request -- SUBJECT authority,
# authorized by the fact that the records are about them, not by any
# measurement. The only shipped composition that ever sets `writable: true`
# is credentialer, so folding the two together meant the deletion right
# docs/CONSENT.md promises in every session could only be reached by also
# handing out full cue-lifecycle authority. See `_SUBJECT_OPERATIONS` below.
_WRITE_OPERATIONS = frozenset(
    {
        "propose_cue",
        "promote_cue",
        "shadow_cue",
        "retire_cue",
        "restore_cue",
        "pin_cue",
        "mute_cue",
        "log_assessment",
    }
)

# operation -> required top-level fields. `operation` itself is the only field the
# JSON schema marks required; everything else is enforced here at runtime because
# JSON Schema cannot portably express "required conditional on operation" across
# providers.
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "status": (),
    "cues": (),
    "why": ("session_id",),
    "observations": (),
    "read_profile": ("provider", "model", "domain"),
    "propose_cue": ("provider", "model", "domain", "text", "origin"),
    "promote_cue": ("provider", "model", "domain", "cue_id", "entry_evidence"),
    "shadow_cue": ("provider", "model", "domain", "cue_id"),
    "retire_cue": ("provider", "model", "domain", "cue_id", "exit_evidence"),
    "restore_cue": ("provider", "model", "domain", "cue_id"),
    "pin_cue": ("provider", "model", "domain", "cue_id"),
    "mute_cue": ("provider", "model", "domain", "cue_id"),
    "log_assessment": (
        "run",
        "provider",
        "model",
        "domain",
        "probes",
        "verdict",
        "n_per_arm",
        "mean",
        "variance",
    ),
    "forget": ("since",),
}

# operation -> OPTIONAL top-level fields _dispatch() reads. Together with
# _REQUIRED_FIELDS this is the complete set of fields an operation consumes,
# which is what lets _build_input_schema() advertise only the properties the
# instance's operations can use.
_OPTIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "cues": ("provider", "model", "domain"),
    "observations": ("session_id", "limit"),
}

# Extra teaching text appended to a "missing required field" error for fields whose
# absence reflects a design invariant worth restating, not just a shape mismatch.
_MISSING_FIELD_HINTS: dict[str, str] = {
    "exit_evidence": (
        "A cue is never removed on judgment alone -- supply an assessment run id with a "
        "confident no-effect verdict."
    ),
    "entry_evidence": (
        "A cue is never promoted on judgment alone -- supply an assessment run id with a "
        "strictly positive, adequately powered verdict."
    ),
    "origin": (
        "A cue must be traceable to observations that actually occurred -- supply the "
        "observation ids it was derived from."
    ),
}

# Fields where 0 (or an empty-looking-but-valid value) is legitimate, not "missing".
_NUMERIC_FIELDS = frozenset({"n_per_arm", "mean", "variance", "limit"})
_LIST_FIELDS = frozenset({"origin", "probes"})

# SUBJECT AUTHORITY -- UNCONDITIONALLY AVAILABLE ON EVERY INSTANCE.
#
# `forget` deletes a user's own observation records because they asked. What
# authorizes it is that the records are ABOUT THEM; no measurement, no
# credential, and no operator decision is involved. So it is gated by nothing:
# not `writable`, not `surface`, not any future config key.
#
# That is a correction of a correction, and both errors are worth recording
# because they were the SAME error. `forget` first sat in _WRITE_OPERATIONS
# behind `writable: true` -- held only by credentialer, so the deletion right
# was unreachable from every shipped session. The first fix moved it behind a
# NEW key, `surface: "consent"`, which fixed the two adoption bundles and left
# the full loop (bundle.md -> behaviors/preceptor.yaml, which sets no surface)
# still unable to reach it -- while shipping context/awareness.md's promise of
# `preceptor forget --since <date>` in that very session. A config knob whose
# value decides whether a person may delete their own data is the wrong shape
# no matter what it is called, and it fails silently by omission: a
# composition that simply does not set the key loses the right with no error,
# which docs/CONSENT.md already says is worse than having no control at all.
#
# There is no composition where "you may be recorded but may not delete" is
# the right answer. If the bundle records, deletion must work. If it does not
# record, `forget` is harmless -- there is nothing to delete. Hence
# unconditional BY CONSTRUCTION, so no composition can lose it by omission.
_SUBJECT_OPERATIONS = frozenset({"forget"})

# Reads are always available too. Derived, not hand-maintained, so it cannot
# drift out of sync as operations are added or reclassified.
_READ_OPERATIONS = frozenset(_REQUIRED_FIELDS) - _WRITE_OPERATIONS - _SUBJECT_OPERATIONS

# `surface` is SCHEMA NARROWING -- a token-cost and ergonomics knob, NEVER an
# authority gate. The full operation set is 14 entries, each with a name and a
# description, and the tool schema is re-sent on every provider request; a
# bundle whose only business with this tool is the recording-consent controls
# should not pay for `promote_cue`/`retire_cue`/`log_assessment` on every turn.
# This repo budgets always-on cost carefully everywhere else (the 500-token
# context policy, `bundle.md` refusing to restate what context files carry) --
# this is the same discipline applied to the tool surface.
#
# Because it is not an authority gate, narrowing NEVER removes an operation
# the caller could otherwise reach: execute() re-checks `writable` for the
# ledger writes and checks nothing at all for reads and `forget`, regardless
# of what a given instance's schema advertises.
#
# _SURFACES is BUILT from _SURFACE_READS by unioning _SUBJECT_OPERATIONS into
# every entry, so a surface that omits `forget` is not something you can write
# by mistake -- it is unrepresentable. Pinned by
# test_every_surface_includes_forget.
_SURFACE_READS: dict[str, frozenset[str]] = {
    # The recording-consent domain, matching context/awareness.md's table:
    #   | What is being recorded? | `preceptor status`              |
    #   | Show my records         | `preceptor observations --mine` |
    #   | Delete records          | `preceptor forget --since ...`  |  <- _SUBJECT
    # Deliberately excludes the DOSING controls (`cues`, `why`) and the
    # ledger internals (`read_profile`): the adoption bundles that use this
    # surface compose no injector and no ledger writer at all.
    "consent": frozenset({"status", "observations"}),
}

_SURFACES: dict[str, frozenset[str]] = {
    name: reads | _SUBJECT_OPERATIONS for name, reads in _SURFACE_READS.items()
}

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": sorted(_REQUIRED_FIELDS),
            "description": "Which preceptor ledger operation to perform.",
        },
        "provider": {
            "type": "string",
            "description": "LLM provider id, e.g. 'anthropic'.",
        },
        "model": {"type": "string", "description": "Model id, e.g. 'claude-opus-5'."},
        "domain": {
            "type": "string",
            "description": "Domain identifier, e.g. 'python-implementation'.",
        },
        "session_id": {"type": "string", "description": "Session id (for 'why')."},
        "limit": {
            "type": "integer",
            "description": "Bound on files scanned (for 'observations').",
        },
        "text": {"type": "string", "description": "Cue text (for 'propose_cue')."},
        "origin": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Observation ids this cue is traceable to.",
        },
        "cue_id": {"type": "string", "description": "Cue id, e.g. 'cue-017'."},
        "entry_evidence": {
            "type": "string",
            "description": "Assessment run id showing a positive delta.",
        },
        "exit_evidence": {
            "type": "string",
            "description": "Assessment run id showing a confident no-effect.",
        },
        "run": {
            "type": "string",
            "description": "Assessment run id (for 'log_assessment').",
        },
        "probes": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Probe identifiers used in this assessment.",
        },
        "verdict": {
            "type": "string",
            "enum": ["positive", "no-effect", "inconclusive"],
            "description": "Pre-registered judgment of the assessment.",
        },
        "n_per_arm": {
            "type": "integer",
            "description": "Runs per arm (minimum 5 to gate anything).",
        },
        "mean": {"type": "number", "description": "Mean effect observed."},
        "variance": {
            "type": "number",
            "description": "Variance of the observed effect.",
        },
        "since": {
            "type": "string",
            "description": "ISO-8601 date/datetime (for 'forget').",
        },
    },
    "required": ["operation"],
}


def _build_input_schema(allowed_operations: frozenset[str]) -> dict[str, Any]:
    """Return a fresh, instance-scoped schema narrowed to this instance.

    Never return (or mutate) `_INPUT_SCHEMA` itself. Multiple `PreceptorTool`
    instances with different `writable`/`surface` configs coexist in the same
    process routinely -- every test module in this package does it, and a
    real deployment does too (the general session's read-only instance
    alongside credentialer's write-mounted one). `_INPUT_SCHEMA` is a nested,
    mutable dict; a property that returned it directly, or shallow-copied
    only the top level, would let one instance's schema silently corrupt
    every other instance's `operation` enum, including one already handed to
    a provider.

    Two narrowings, both hints to the calling model, neither a security
    boundary -- execute() re-checks every gate and never consults the schema:

    - the `operation` enum lists what THIS instance can actually do;
    - `properties` lists only fields those operations consume
      (_REQUIRED_FIELDS | _OPTIONAL_FIELDS). The schema is re-sent on every
      provider request, and the read-only instance in every full-loop
      session was paying for eleven write-only fields (text, origin,
      entry/exit_evidence, verdict, n_per_arm, ...) it rejects anyway.
    """
    used: set[str] = {"operation"}
    for op in allowed_operations:
        used.update(_REQUIRED_FIELDS.get(op, ()))
        used.update(_OPTIONAL_FIELDS.get(op, ()))
    schema = copy.deepcopy(_INPUT_SCHEMA)
    schema["properties"] = {k: v for k, v in schema["properties"].items() if k in used}
    schema["properties"]["operation"]["enum"] = sorted(allowed_operations)
    return schema


def _get_capability_safe(coordinator: Any, name: str) -> Any:
    if coordinator is None:
        return None
    get_capability = getattr(coordinator, "get_capability", None)
    if not callable(get_capability):
        return None
    try:
        return get_capability(name)
    except Exception:  # noqa: BLE001 -- capability lookup must never break mount()
        return None


def _project_slug(coordinator: Any) -> str:
    """Derive the `{project}` slug from the session working directory.

    THIS FUNCTION IS DUPLICATED VERBATIM IN THREE MODULES and must stay
    byte-identical in all of them:

        modules/tool-preceptor/.../__init__.py        (here)
        modules/hooks-trajectory-observer/.../__init__.py
        modules/hooks-cue-injector/.../__init__.py

    The duplication is deliberate -- AGENTS.md requires flat, independent
    modules with no cross-imports -- so the agreement is enforced by
    `tests/test_project_slug_agreement.py` at the repo root, which loads all
    three and asserts they return the same slug for the same input. Change
    one, change all three, or that test fails.

    WHY IT MATTERS: these three modules must resolve `{project}` in
    `~/.amplifier/projects/{project}/preceptor` to the SAME directory. They
    did not. The observer WROTE to `.../projects/project/preceptor` while
    this tool READ from `.../projects/-root-project/preceptor`, and the
    injector used a third form again (`root-project`). Measured live in a
    Digital Twin: 17 observation records on disk, `observations` reporting
    `total_observations: 0`, and `forget` returning success having deleted
    nothing. A privacy control that reports success while touching the wrong
    directory is worse than one that plainly does not exist.

    WHY THE DASHED FORM, not `Path(working_dir).name`:

      1. Amplifier core already uses it. `/root/.amplifier/projects/
         -root-project/` exists in a live container as core's own session
         directory, so this convention is the ecosystem's, not ours.
      2. `.name` COLLIDES. `/home/alice/project` and `/home/bob/project`
         both yield `project`, so two unrelated checkouts would share one
         observation store and one ledger -- meaning one person's records
         are readable, and deletable, from the other's session. For a store
         holding per-session behavioural records that is a defect, not an
         inconvenience.
    """
    working_dir = _get_capability_safe(coordinator, "session.working_dir")
    if not working_dir:
        return "default"
    slug = str(working_dir).replace("\\", "-").replace("/", "-").replace(":", "")
    return slug or "default"


def _working_dir(coordinator: Any) -> Path:
    working_dir = _get_capability_safe(coordinator, "session.working_dir")
    if working_dir:
        return Path(working_dir)
    return Path.cwd()


def _is_missing(field: str, data: dict[str, Any]) -> bool:
    if field not in data:
        return True
    value = data[field]
    if value is None:
        return True
    if field in _NUMERIC_FIELDS:
        return False  # present and not None -- 0 is a legitimate value
    if field in _LIST_FIELDS:
        return not isinstance(value, list) or len(value) == 0
    if isinstance(value, str):
        return value.strip() == ""
    return False


class PreceptorTool:
    """The `preceptor` tool: reads and (when writable) mutates the evidence-gated
    cue ledger. See README.md for the full operation reference."""

    def __init__(self, coordinator: Any, config: dict[str, Any]) -> None:
        self._coordinator = coordinator
        self._writable = config.get("writable", False) is True
        # Preserve untrusted autonomy inputs for the strict authority predicate.
        # Coercing "false" or "NaN" here would bypass its validation.
        self._autonomous = config.get("autonomous", False)
        self._false_fade_ceiling = config.get("false_fade_ceiling", 0.10)
        self._min_fade_attempts = config.get("min_fade_attempts", 40)
        self._shadow_window_days = int(config.get("shadow_window_days", 30))
        self._shadow_window_opportunities = int(
            config.get("shadow_window_opportunities", 40)
        )
        self._max_active_cues = int(config.get("max_active_cues", 8))
        self._max_cue_chars = int(config.get("max_cue_chars", 200))

        # `surface` selects WHICH operations this instance advertises in its
        # JSON schema. It never decides what the instance may DO -- see
        # _SURFACES. Every branch below includes _SUBJECT_OPERATIONS, and
        # execute() does not consult `surface` at all.
        surface: str | None = config.get("surface")
        self._surface = surface
        if surface is None:
            advertised = _READ_OPERATIONS | _SUBJECT_OPERATIONS
        elif surface in _SURFACES:
            advertised = _SURFACES[surface]
        else:
            valid = ", ".join(sorted(_SURFACES))
            raise ValueError(
                f"Unknown surface {surface!r} for tool-preceptor. Valid "
                f"surfaces: {valid}. An unrecognized surface fails LOUDLY at "
                "mount time rather than silently falling back to some other "
                "operation set -- a typo would otherwise change what the "
                "model is told this tool can do, with no error anywhere. "
                "(It could never cost a user their deletion right: `forget` "
                "is unconditional and is in every surface by construction.)"
            )

        if self._writable:
            advertised = advertised | _WRITE_OPERATIONS
        self._schema = _build_input_schema(frozenset(advertised))

        project_slug = _project_slug(coordinator)
        self._root = ledger.resolve_root(
            config.get("root", _DEFAULT_ROOT), project_slug
        )
        self._working_dir = _working_dir(coordinator)

    @property
    def name(self) -> str:
        return "preceptor"

    @property
    def description(self) -> str:
        # Branched so the model is told what THIS instance advertises, not an
        # aspirational full command list -- same principle _build_input_schema
        # applies to the schema's `operation` enum. EVERY branch states that
        # deleting your own records works, because on every branch it does.
        if self._writable:
            return (
                "Read and mutate the preceptor evidence-gated cue ledger "
                "(writable): propose, promote, shadow, retire, restore, pin, or "
                "mute per-model/domain cues on measured evidence; log "
                "assessments; forget deletes the user's own observation records."
            )
        if self._surface == "consent":
            return (
                "Preceptor recording controls: status (what is recorded), "
                "observations (summary of records about the user), forget "
                "(delete them). No cue-ledger operations on this instance."
            )
        return (
            "Read the preceptor evidence-gated cue ledger: per-model/domain "
            "cues changed only on measured evidence. forget deletes the user's "
            "own observation records. Read-only: ledger writes need the "
            "credentialer agent's writable instance."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return self._schema

    @property
    def writable(self) -> bool:
        return self._writable

    @property
    def surface(self) -> str | None:
        return self._surface

    @property
    def _autonomy_cfg(self) -> dict[str, Any]:
        return {
            "autonomous": self._autonomous,
            "min_fade_attempts": self._min_fade_attempts,
            "false_fade_ceiling": self._false_fade_ceiling,
        }

    async def execute(self, input: dict[str, Any]) -> ToolResult:
        if ToolResult is None:
            raise RuntimeError(
                "The Amplifier tool adapter requires amplifier-core; use project-preceptor for standalone ledger access."
            )
        try:
            operation = input.get("operation")
            if not isinstance(operation, str) or not operation:
                return ToolResult(
                    success=False,
                    error={"message": "operation is required and must be a string."},
                )
            if operation not in _REQUIRED_FIELDS:
                valid = ", ".join(sorted(_REQUIRED_FIELDS))
                return ToolResult(
                    success=False,
                    error={
                        "message": f"Unknown operation {operation!r}. Valid operations: {valid}."
                    },
                )

            missing = [f for f in _REQUIRED_FIELDS[operation] if _is_missing(f, input)]
            if missing:
                hints = " ".join(
                    _MISSING_FIELD_HINTS[f]
                    for f in missing
                    if f in _MISSING_FIELD_HINTS
                )
                message = f"Operation {operation!r} requires {missing}."
                if hints:
                    message = f"{message} {hints}"
                return ToolResult(success=False, error={"message": message})

            if operation in _WRITE_OPERATIONS and not self._writable:
                return ToolResult(
                    success=False,
                    error={
                        "message": (
                            f"Operation {operation!r} requires write access to the preceptor "
                            "ledger. Only the credentialer agent holds write access "
                            "(writable: true) -- this instance is read-only."
                        )
                    },
                )

            # NOTHING GATES _SUBJECT_OPERATIONS, AND NOTHING MAY. There is no
            # second check here on purpose -- not on `surface`, not on
            # `writable`, not on a future key. A person's right to delete
            # records about themselves is not an operator's setting, and a
            # gate on it fails silently by omission: a composition that just
            # never sets the key loses the right with no error. Two gates
            # have already been tried and removed for exactly that reason
            # (`writable`, then `surface`); see _SUBJECT_OPERATIONS above for
            # both, and test_forget_is_gated_by_neither_writable_nor_surface,
            # which fails if a third one appears.
            return await self._dispatch(operation, input)
        except ledger.LedgerError as exc:
            return ToolResult(success=False, error={"message": str(exc)})
        except Exception as exc:
            logger.exception("preceptor tool operation failed")
            return ToolResult(
                success=False, error={"message": f"Unexpected error: {exc}"}
            )

    async def _dispatch(self, operation: str, data: dict[str, Any]) -> ToolResult:
        if ToolResult is None:
            raise RuntimeError(
                "The Amplifier tool adapter requires amplifier-core; use project-preceptor for standalone ledger access."
            )
        root = self._root
        wd = self._working_dir

        if operation == "status":
            return ToolResult(
                success=True, output=ledger.read_status(root, working_dir=wd)
            )

        if operation == "cues":
            cues = ledger.list_cues(
                root,
                data.get("provider"),
                data.get("model"),
                data.get("domain"),
                working_dir=wd,
            )
            return ToolResult(success=True, output={"cues": cues})

        if operation == "why":
            manifest = ledger.read_manifest(root, data["session_id"])
            return ToolResult(success=True, output=manifest)

        if operation == "observations":
            summary = ledger.summarize_observations(
                root, data.get("session_id"), data.get("limit")
            )
            return ToolResult(success=True, output=summary)

        if operation == "read_profile":
            doc = ledger.read_profile(
                root, data["provider"], data["model"], data["domain"], working_dir=wd
            )
            return ToolResult(success=True, output=doc)

        if operation == "propose_cue":
            cue = ledger.propose_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["text"],
                data["origin"],
                max_cue_chars=self._max_cue_chars,
                max_active_cues=self._max_active_cues,
                working_dir=wd,
            )
            return ToolResult(success=True, output=cue)

        if operation == "promote_cue":
            unlocked, reason = gates.autonomy_unlocked(
                ledger.read_state(root), self._autonomy_cfg
            )
            cue = ledger.promote_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["cue_id"],
                data["entry_evidence"],
                max_active_cues=self._max_active_cues,
                working_dir=wd,
                dry_run=not unlocked,
            )
            if unlocked:
                return ToolResult(success=True, output=cue)
            return ToolResult(
                success=True,
                output={"locked": True, "reason": reason, "proposed_diff": cue},
            )

        if operation == "shadow_cue":
            cue = ledger.shadow_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["cue_id"],
                cfg=self._autonomy_cfg,
                working_dir=wd,
            )
            return ToolResult(success=True, output=cue)

        if operation == "retire_cue":
            unlocked, reason = gates.autonomy_unlocked(
                ledger.read_state(root), self._autonomy_cfg
            )
            cue = ledger.retire_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["cue_id"],
                data["exit_evidence"],
                working_dir=wd,
                dry_run=not unlocked,
            )
            if unlocked:
                return ToolResult(success=True, output=cue)
            return ToolResult(
                success=True,
                output={"locked": True, "reason": reason, "proposed_diff": cue},
            )

        if operation == "restore_cue":
            cue = ledger.restore_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["cue_id"],
                cfg=self._autonomy_cfg,
                working_dir=wd,
            )
            return ToolResult(success=True, output=cue)

        if operation == "pin_cue":
            cue = ledger.pin_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["cue_id"],
                working_dir=wd,
            )
            return ToolResult(success=True, output=cue)

        if operation == "mute_cue":
            cue = ledger.mute_cue(
                root,
                data["provider"],
                data["model"],
                data["domain"],
                data["cue_id"],
                working_dir=wd,
            )
            return ToolResult(success=True, output=cue)

        if operation == "log_assessment":
            record = ledger.log_assessment(
                root,
                data["run"],
                data["provider"],
                data["model"],
                data["domain"],
                data["probes"],
                data["verdict"],
                data["n_per_arm"],
                data["mean"],
                data["variance"],
            )
            return ToolResult(success=True, output=record)

        if operation == "forget":
            result = ledger.forget(root, data["since"])
            return ToolResult(success=True, output=result)

        # Unreachable: `operation` was already validated against _REQUIRED_FIELDS above.
        raise AssertionError(f"unhandled operation {operation!r}")


async def mount(
    coordinator: Any, config: dict[str, Any] | None = None
) -> dict[str, Any]:
    config = config or {}
    tool = PreceptorTool(coordinator, config)
    await coordinator.mount("tools", tool, name=tool.name)  # REQUIRED
    return {
        "name": "tool-preceptor",
        "version": "0.1.0",
        "provides": ["preceptor"],
        # `writable` is the ledger-write authority this instance holds;
        # `surface` is only which operation set it advertises. Neither
        # affects `forget`, which every instance can execute.
        "writable": tool.writable,
        "surface": tool.surface,
    }

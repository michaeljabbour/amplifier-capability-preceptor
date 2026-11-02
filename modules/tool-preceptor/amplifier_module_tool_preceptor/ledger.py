"""On-disk ledger layer for the preceptor tool.

Implements exactly the file layout from
``context/methodology/ledger-format.md`` -- the authoritative format spec
that the observer hook, the injector hook, and this tool all depend on::

    <root>/observations/<session_id>.jsonl
    <root>/manifests/<session_id>.json
    <root>/ledger/<provider>/<model>/<domain>.yaml
    <root>/assessments/<run_id>.json
    <root>/state.json

No module other than this one writes these files. This module is the only
writer -- it has no dependency on ``amplifier_core`` and does no LLM
reasoning; it is a pure filesystem + git subprocess layer. Cue-lifecycle
*decision* logic (the evidence predicates) lives in ``gates.py``, imported
here and never duplicated.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomllib
import yaml

from . import gates

logger = logging.getLogger(__name__)

CUE_TEXT_MAX_DEFAULT = 200

# Ledger content is untrusted input: the injector hook places it directly into a
# live session. These patterns catch tool invocation and privilege-escalation
# attempts (evidence-standards.md, "Cue text is untrusted input").
_FORBIDDEN_CUE_PATTERNS: tuple[str, ...] = (
    "bash",
    "<tool",
    "approve",
    "permission",
    "sudo",
    "ignore previous",
    "system prompt",
    "</",
)

_VALID_VERDICTS: tuple[str, ...] = ("positive", "no-effect", "inconclusive")

_LOCK_STALE_SECONDS = 30.0
_LOCK_RETRY_SECONDS = 0.05
_LOCK_TIMEOUT_SECONDS = 5.0

_VERSION_RE = re.compile(r"(\d+)")


# ---------------------------------------------------------------------------
# Errors -- messages are written to teach, not just to fail.
# ---------------------------------------------------------------------------


class LedgerError(Exception):
    """Base for every ledger-layer failure."""


class UnresolvedReferenceError(LedgerError):
    """An origin/entry/exit evidence reference does not resolve on disk.

    An id that does not resolve is a hard failure, not a warning: an
    unresolvable evidence reference is worse than no reference, because it
    looks like a gate passed (evidence-standards.md).
    """


class CueTextError(LedgerError):
    """Cue text failed validation (type, length, or forbidden pattern)."""


class CueStateError(LedgerError):
    """A requested state transition is not legal from the cue's current state,
    or the evidence provided does not support it."""


class CueBudgetError(LedgerError):
    """Applying this write would exceed max_active_cues."""


class LockTimeoutError(LedgerError):
    """Timed out waiting for the on-disk advisory lock."""


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def resolve_root(root: str, project_slug: str | None = None) -> Path:
    """Resolve the configured ``root`` string to a concrete, expanded Path.

    Supports the ``{project}`` placeholder used in the default config value
    (``~/.amplifier/projects/{project}/preceptor``). Callers resolve the
    slug themselves (see ``__init__.py:_project_slug``) so this module stays
    free of any coordinator/session coupling and is trivially testable.
    """
    text = root
    if "{project}" in text:
        text = text.replace("{project}", project_slug or "default")
    return Path(text).expanduser()


def observations_dir(root: Path) -> Path:
    return root / "observations"


def manifests_dir(root: Path) -> Path:
    return root / "manifests"


def ledger_dir(root: Path) -> Path:
    return root / "ledger"


def assessments_dir(root: Path) -> Path:
    return root / "assessments"


def state_path(root: Path) -> Path:
    return root / "state.json"


def ledger_doc_path(root: Path, provider: str, model: str, domain: str) -> Path:
    return ledger_dir(root) / provider / model / f"{domain}.yaml"


def manifest_path(root: Path, session_id: str) -> Path:
    return manifests_dir(root) / f"{session_id}.json"


def observation_path(root: Path, session_id: str) -> Path:
    return observations_dir(root) / f"{session_id}.jsonl"


def assessment_path(root: Path, run_id: str) -> Path:
    return assessments_dir(root) / f"{run_id}.json"


# ---------------------------------------------------------------------------
# Low-level: atomic writes, advisory locking, git commit
# ---------------------------------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Write to a temp file in the same directory, then os.replace(). Never leaves
    a partially-written file at `path`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@contextlib.contextmanager
def _locked(path: Path, timeout: float = _LOCK_TIMEOUT_SECONDS):
    """Advisory lock via a sibling ``<file>.lock`` file.

    Acquisition is atomic (O_CREAT|O_EXCL) even across processes, which is
    what makes this safe for multiple concurrent sessions writing the same
    ledger document. A lock older than ``_LOCK_STALE_SECONDS`` is assumed
    abandoned by a crashed process and is reclaimed.
    """
    lock_path = path.parent / f"{path.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_SECONDS:
                with contextlib.suppress(OSError):
                    os.unlink(lock_path)
                continue
            if time.monotonic() > deadline:
                raise LockTimeoutError(
                    f"Timed out waiting for lock on {path}"
                ) from None
            time.sleep(_LOCK_RETRY_SECONDS)
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            os.unlink(lock_path)


def _is_git_repo(root: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def _git_commit(root: Path, paths: list[Path], message: str) -> None:
    """Best-effort single-commit-per-mutation. Never raises: if git is absent,
    unconfigured, or the dir is not a repo, the write must still succeed."""
    try:
        if not root.exists() or not _is_git_repo(root):
            return
        rel_paths: list[str] = []
        for p in paths:
            try:
                rel_paths.append(str(p.relative_to(root)))
            except ValueError:
                rel_paths.append(str(p))
        subprocess.run(
            ["git", "-C", str(root), "add", *rel_paths],
            capture_output=True,
            timeout=5,
            check=False,
        )
        subprocess.run(
            ["git", "-C", str(root), "commit", "-m", message],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("preceptor: git commit skipped for %s", paths, exc_info=True)


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 date/datetime, ALWAYS returning a timezone-aware value.

    The localisation on the last line is the whole point. Without it, this
    returned whatever `fromisoformat` produced -- aware for `...Z` and
    `...+00:00`, NAIVE for a bare `2026-08-01` or `2026-08-01T00:00:00` --
    and every naive result was a landmine, because Python refuses to compare
    naive and aware datetimes at all:

        TypeError: can't compare offset-naive and offset-aware datetimes

    `forget()` is the one place that compares timestamps, and it compares a
    user-supplied `since` against each record's `ts`. The observer writes
    `ts` with `datetime.now(timezone.utc).isoformat()` -- always AWARE. So a
    bare date crashed the operation outright, and a bare date is precisely
    what `docs/CONSENT.md` documents: `preceptor forget --since <date>`.
    The documented control did not work on the documented invocation.
    Verified live: two of two natural asks ("delete everything from this
    month onward") produced the TypeError, and only succeeded because the
    model retried on its own initiative with an explicit `+00:00`.

    Naive input is interpreted as UTC rather than as local time. That is the
    correct reading here and not merely the convenient one: every timestamp
    this module compares against was written in UTC (the observer's
    `isoformat()` of an aware UTC value, and this module's own `_utcnow()`,
    which emits a trailing `Z`). Interpreting a bare date as local time
    would silently shift the deletion boundary by the operator's offset --
    for a deletion boundary, deleting a few hours' more or fewer records
    than the user asked for is the failure mode to avoid.

    Because BOTH sides of that comparison come through this function, this
    also closes the mirror-image bug that was never reported: an AWARE
    `since` against a NAIVE record `ts`, which would have raised the same
    TypeError if any writer ever emitted a naive timestamp.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LedgerError(f"{value!r} is not a valid ISO-8601 date/datetime.") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _iter_jsonl(path: Path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------


def compute_model_fingerprint(
    provider: str, model: str, build_id: str | None = None
) -> str:
    """sha256 of the provider+model identity, plus any provider-reported build id.

    The tool's operation surface only receives provider/model as strings (no
    live Provider instance), so ``build_id`` is best-effort and will usually
    be absent -- this is an honest limitation of what's resolvable at this
    layer, documented rather than silently pretended away.
    """
    payload = f"{provider}:{model}:{build_id or ''}"
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_environment_fingerprint(
    domain: str, working_dir: Path | None = None
) -> str:
    """sha256 over sorted dependency major versions found in the working dir
    (pyproject.toml / package.json if present) plus the domain name."""
    working_dir = working_dir or Path.cwd()
    majors = _collect_dependency_majors(working_dir)
    payload = domain + ":" + ",".join(sorted(majors))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collect_dependency_majors(working_dir: Path) -> list[str]:
    majors: list[str] = []
    pyproject = working_dir / "pyproject.toml"
    if pyproject.is_file():
        majors.extend(_pyproject_majors(pyproject))
    package_json = working_dir / "package.json"
    if package_json.is_file():
        majors.extend(_package_json_majors(package_json))
    return majors


def _pyproject_majors(path: Path) -> list[str]:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return []
    deps = data.get("project", {}).get("dependencies", []) or []
    out: list[str] = []
    for dep in deps:
        name, _, spec = dep.partition(">=")
        if not spec:
            name, _, spec = dep.partition("==")
        match = _VERSION_RE.search(spec) if spec else None
        major = match.group(1) if match else "0"
        out.append(f"{name.strip()}:{major}")
    return out


def _package_json_majors(path: Path) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    out: list[str] = []
    for section in ("dependencies", "devDependencies"):
        for name, version in (data.get(section) or {}).items():
            match = _VERSION_RE.search(str(version))
            major = match.group(1) if match else "0"
            out.append(f"{name}:{major}")
    return out


def _derive_supervision(doc: dict[str, Any]) -> str:
    """DERIVED and decorative only. Nothing may read this to relax an approval
    gate, widen a permission, or skip a check (ledger-format.md)."""
    active = sum(1 for c in doc.get("cues", []) if c.get("status") == "active")
    if active == 0:
        return "none"
    if active <= 2:
        return "light"
    if active <= 5:
        return "medium"
    return "heavy"


# ---------------------------------------------------------------------------
# Cue text validation
# ---------------------------------------------------------------------------


def validate_cue_text(text: Any, max_chars: int = CUE_TEXT_MAX_DEFAULT) -> None:
    """Raise CueTextError if ``text`` is unsafe for injection into a live session.

    Ledger content is untrusted input: it is read by the injector and placed
    directly into a live session's context. This is an injection-vector
    check, not a style check.
    """
    if not isinstance(text, str):
        raise CueTextError(f"Cue text must be a string, got {type(text).__name__}.")
    if not text.strip():
        raise CueTextError("Cue text must not be empty.")
    if len(text) > max_chars:
        raise CueTextError(
            f"Cue text is {len(text)} characters, exceeding max_cue_chars ({max_chars}). "
            "Cues are dosed into every session; keep them short and specific."
        )
    lowered = text.lower()
    for pattern in _FORBIDDEN_CUE_PATTERNS:
        if pattern in lowered:
            raise CueTextError(
                f"Cue text contains a disallowed pattern ({pattern!r}). Cue text is "
                "untrusted input injected into a live session and must never invoke "
                "tools, touch permissions, or redefine the cue protocol itself."
            )


# ---------------------------------------------------------------------------
# Evidence resolution
# ---------------------------------------------------------------------------


def resolve_observation_ids(root: Path, ids: list[str]) -> None:
    """Raise UnresolvedReferenceError if any observation id does not exist on disk."""
    if not ids:
        raise UnresolvedReferenceError(
            "origin must be a non-empty list of observation ids."
        )
    missing = set(ids)
    obs_dir = observations_dir(root)
    if obs_dir.is_dir():
        for file in obs_dir.glob("*.jsonl"):
            if not missing:
                break
            for record in _iter_jsonl(file):
                obs_id = record.get("id")
                if obs_id in missing:
                    missing.discard(obs_id)
    if missing:
        raise UnresolvedReferenceError(
            f"origin references unresolved observation id(s): {sorted(missing)}. An id "
            "that does not resolve on disk is a hard failure, not a warning -- it would "
            "look like a gate passed."
        )


def load_assessment(root: Path, run_id: str) -> dict[str, Any]:
    path = assessment_path(root, run_id)
    if not path.exists():
        raise UnresolvedReferenceError(
            f"Evidence reference {run_id!r} does not resolve to a recorded assessment. An "
            "unresolvable evidence reference is worse than none -- it looks like a gate "
            "passed. Call log_assessment first."
        )
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# state.json
# ---------------------------------------------------------------------------


def _default_state() -> dict[str, Any]:
    return {
        "v": 1,
        "fade_attempts": 0,
        "shadow_restores": 0,
        "false_fade_rate": 0.0,
        "detector_calibrated": False,
        "detector_precision": None,
        "detector_recall": None,
        "autonomy_unlocked": False,
    }


def read_state(root: Path) -> dict[str, Any]:
    """Initialize a new ledger, but never invent evidence in an existing one.

    Missing fields in a stored state must reach the authority gate as missing.
    Corrupt or unsupported storage carries no usable evidence and stays locked.
    """
    path = state_path(root)
    if not path.exists():
        return _default_state()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeError):
        logger.warning("preceptor: unreadable state; autonomy remains locked")
        return {}
    if not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != 1:
        logger.warning("preceptor: unsupported state schema; autonomy remains locked")
        return {}
    return data


def write_state(root: Path, state: dict[str, Any]) -> None:
    _atomic_write_text(
        state_path(root), json.dumps(state, indent=2, sort_keys=False) + "\n"
    )
    _git_commit(root, [state_path(root)], "state: update trust counters")


def update_state(
    root: Path,
    cfg: dict[str, Any],
    *,
    fade_attempts_delta: int = 0,
    shadow_restores_delta: int = 0,
) -> dict[str, Any]:
    """Read-modify-write state.json under the file lock, recomputing
    false_fade_rate (which may re-engage the autonomy lock -- that is
    correct behavior, not a bug)."""
    with _locked(state_path(root)):
        state = read_state(root)
        for field, delta in (
            ("fade_attempts", fade_attempts_delta),
            ("shadow_restores", shadow_restores_delta),
        ):
            if not gates.is_counter(state.get(field)) or not gates.is_counter(delta):
                raise LedgerError(
                    f"{field} and its delta must be nonnegative integers; "
                    "invalid history cannot be converted into measured evidence."
                )
        state["fade_attempts"] += fade_attempts_delta
        state["shadow_restores"] += shadow_restores_delta
        state["false_fade_rate"] = gates.compute_false_fade_rate(
            state["shadow_restores"], state["fade_attempts"]
        )
        unlocked, _reason = gates.autonomy_unlocked(state, cfg)
        state["autonomy_unlocked"] = unlocked
        write_state(root, state)
        return state


# ---------------------------------------------------------------------------
# Ledger documents
# ---------------------------------------------------------------------------


def _empty_ledger_doc(provider: str, model: str, domain: str) -> dict[str, Any]:
    return {
        "v": 1,
        "provider": provider,
        "model": model,
        "model_fingerprint": None,
        "domain": domain,
        "environment_fingerprint": None,
        "version": 0,
        "supervision": "none",
        "cues": [],
    }


def _find_cue(doc: dict[str, Any], cue_id: str) -> dict[str, Any] | None:
    for cue in doc.get("cues", []):
        if cue.get("id") == cue_id:
            return cue
    return None


def _count_active(doc: dict[str, Any]) -> int:
    return sum(1 for c in doc.get("cues", []) if c.get("status") == "active")


def _next_cue_id(doc: dict[str, Any]) -> str:
    existing = {c.get("id", "") for c in doc.get("cues", [])}
    n = 1
    while f"cue-{n:03d}" in existing:
        n += 1
    return f"cue-{n:03d}"


def _write_ledger_doc_raw(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    doc: dict[str, Any],
    *,
    commit_message: str,
) -> None:
    path = ledger_doc_path(root, provider, model, domain)
    with _locked(path):
        text = yaml.safe_dump(doc, sort_keys=False)
        _atomic_write_text(path, text)
        _git_commit(root, [path], commit_message)


def _stamp_and_write(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    doc: dict[str, Any],
    *,
    working_dir: Path | None,
    commit_message: str,
) -> None:
    doc["v"] = doc.get("v", 1)
    doc["provider"] = provider
    doc["model"] = model
    doc["domain"] = domain
    doc["model_fingerprint"] = compute_model_fingerprint(provider, model)
    doc["environment_fingerprint"] = compute_environment_fingerprint(
        domain, working_dir
    )
    doc["version"] = int(doc.get("version", 0)) + 1
    doc["supervision"] = _derive_supervision(doc)
    _write_ledger_doc_raw(
        root, provider, model, domain, doc, commit_message=commit_message
    )


def read_ledger_doc(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    *,
    working_dir: Path | None = None,
    heal: bool = True,
) -> dict[str, Any]:
    """Load a ledger document, reconciling fingerprints on every read.

    A model or environment change invalidates existing evidence immediately.
    Per cue-lifecycle.md: "do not wait for violations to reappear -- waiting
    means the system learns a retirement was wrong by letting the failure
    hit the user." This check runs on every read regardless of the
    `writable` config, because it is a deterministic integrity correction,
    not a cue-lifecycle judgment call.
    """
    path = ledger_doc_path(root, provider, model, domain)
    if not path.exists():
        return _empty_ledger_doc(provider, model, domain)
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or _empty_ledger_doc(provider, model, domain)
    doc.setdefault("cues", [])

    if not heal:
        return doc

    current_model_fp = compute_model_fingerprint(provider, model)
    current_env_fp = compute_environment_fingerprint(domain, working_dir)
    prior_model_fp = doc.get("model_fingerprint")
    prior_env_fp = doc.get("environment_fingerprint")

    changed = False
    if prior_model_fp and prior_model_fp != current_model_fp:
        # Model swapped behind a stable identifier: every cue returns to shadowed
        # for revalidation.
        for cue in doc["cues"]:
            if cue.get("status") != "shadowed":
                cue["status"] = "shadowed"
                cue["shadowed_at"] = _utcnow()
        changed = True

    if prior_env_fp and prior_env_fp != current_env_fp:
        # Environment moved: a cue retired under the prior environment is not
        # evidence about this one. Every faded cue returns to shadowed.
        for cue in doc["cues"]:
            if cue.get("status") == "faded":
                cue["status"] = "shadowed"
                cue["shadowed_at"] = _utcnow()
        changed = True

    doc["model_fingerprint"] = current_model_fp
    doc["environment_fingerprint"] = current_env_fp

    if changed:
        doc["version"] = int(doc.get("version", 0)) + 1
        doc["supervision"] = _derive_supervision(doc)
        _write_ledger_doc_raw(
            root,
            provider,
            model,
            domain,
            doc,
            commit_message=f"ledger: fingerprint reconciliation ({provider}/{model}/{domain})",
        )
    return doc


def read_profile(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    path = ledger_doc_path(root, provider, model, domain)
    if not path.exists():
        raise LedgerError(
            f"No ledger exists yet for {provider}/{model}/{domain}. Propose a cue first "
            "via propose_cue to create the document."
        )
    return read_ledger_doc(root, provider, model, domain, working_dir=working_dir)


def read_status(root: Path, *, working_dir: Path | None = None) -> dict[str, Any]:
    state = read_state(root)
    per_triple: dict[str, dict[str, int]] = {}
    root_ledger = ledger_dir(root)
    if root_ledger.is_dir():
        for doc_path in sorted(root_ledger.rglob("*.yaml")):
            rel = doc_path.relative_to(root_ledger)
            if len(rel.parts) != 3:
                continue
            provider, model, domain_file = rel.parts
            domain = domain_file.removesuffix(".yaml")
            doc = read_ledger_doc(
                root, provider, model, domain, working_dir=working_dir
            )
            counts: dict[str, int] = {}
            for cue in doc.get("cues", []):
                status = cue.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
            per_triple[f"{provider}/{model}/{domain}"] = counts
    return {
        "autonomy_unlocked": state.get("autonomy_unlocked", False),
        "false_fade_rate": state.get("false_fade_rate", 0.0),
        "detector_calibrated": state.get("detector_calibrated", False),
        "fade_attempts": state.get("fade_attempts", 0),
        "shadow_restores": state.get("shadow_restores", 0),
        "cue_counts_by_triple": per_triple,
    }


def list_cues(
    root: Path,
    provider: str | None = None,
    model: str | None = None,
    domain: str | None = None,
    *,
    working_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Active + shadowed cues with counters, optionally filtered by triple."""
    results: list[dict[str, Any]] = []
    root_ledger = ledger_dir(root)
    triples: list[tuple[str, str, str]]
    if provider and model and domain:
        triples = [(provider, model, domain)]
    else:
        triples = []
        if root_ledger.is_dir():
            for doc_path in sorted(root_ledger.rglob("*.yaml")):
                rel = doc_path.relative_to(root_ledger)
                if len(rel.parts) != 3:
                    continue
                p, m, domain_file = rel.parts
                d = domain_file.removesuffix(".yaml")
                if provider and p != provider:
                    continue
                if model and m != model:
                    continue
                if domain and d != domain:
                    continue
                triples.append((p, m, d))

    for p, m, d in triples:
        doc = read_ledger_doc(root, p, m, d, working_dir=working_dir)
        for cue in doc.get("cues", []):
            if cue.get("status") in ("active", "shadowed"):
                entry = dict(cue)
                entry["provider"] = p
                entry["model"] = m
                entry["domain"] = d
                results.append(entry)
    return results


def read_manifest(root: Path, session_id: str) -> dict[str, Any]:
    """Read the immutable manifest for a session. NEVER touches the live ledger --
    this is what answers "why did it do that" after a cue has since been retired."""
    path = manifest_path(root, session_id)
    if not path.exists():
        raise LedgerError(f"No manifest found for session {session_id!r} at {path}.")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def summarize_observations(
    root: Path, session_id: str | None = None, limit: int | None = None
) -> dict[str, Any]:
    """Summary counts only -- never raw records (observations are structural-only
    already, but this operation never dumps even that; see ledger-format.md).

    When `session_id` is given, summarizes just that session's file. Otherwise
    scans observation files newest-first, bounded by `limit` files when given.
    """
    obs_dir = observations_dir(root)
    files: list[Path]
    if session_id:
        files = [observation_path(root, session_id)]
    else:
        files = (
            sorted(obs_dir.glob("*.jsonl"), reverse=True) if obs_dir.is_dir() else []
        )
        if limit:
            files = files[:limit]

    total = 0
    ok_count = 0
    by_event: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    cue_ids_seen: set[str] = set()
    sessions_scanned = 0

    for file in files:
        if not file.exists():
            continue
        sessions_scanned += 1
        for record in _iter_jsonl(file):
            total += 1
            if record.get("ok"):
                ok_count += 1
            event = record.get("event", "unknown")
            by_event[event] = by_event.get(event, 0) + 1
            tool_name = record.get("tool_name")
            if tool_name:
                by_tool[tool_name] = by_tool.get(tool_name, 0) + 1
            for cid in record.get("cue_ids_dosed") or []:
                cue_ids_seen.add(cid)

    return {
        "sessions_scanned": sessions_scanned,
        "total_observations": total,
        "ok_count": ok_count,
        "by_event": by_event,
        "by_tool_name": by_tool,
        "cue_ids_dosed": sorted(cue_ids_seen),
    }


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


def propose_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    text: str,
    origin: list[str],
    *,
    max_cue_chars: int,
    max_active_cues: int,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    validate_cue_text(text, max_cue_chars)
    resolve_observation_ids(root, origin)

    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    if _count_active(doc) >= max_active_cues:
        raise CueBudgetError(
            f"Proposing a new cue would risk exceeding max_active_cues ({max_active_cues}); "
            "active cue count is already at the ceiling. Retire or shadow an existing cue "
            "first -- promotion has no other ceiling."
        )

    cue = {
        "id": _next_cue_id(doc),
        "text": text,
        "status": "proposed",
        "origin_class": "observed",
        "origin": list(origin),
        "entry_evidence": None,
        "exit_evidence": None,
        "unsupported": False,
        "opportunities": 0,
        "violations_recent": 0,
        "pinned": False,
        "dosed_at": None,
        "shadowed_at": None,
        "created": _utcnow(),
    }
    doc.setdefault("cues", []).append(cue)
    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"propose {cue['id']}: origin={origin}",
    )
    return dict(cue)


def _promote_rejection_message(entry_evidence: str, assessment: dict[str, Any]) -> str:
    return (
        f"Assessment {entry_evidence!r} does not support promotion: promote_cue requires "
        f"verdict == 'positive' and n_per_arm >= {gates.MIN_N_PER_ARM} (got "
        f"verdict={assessment.get('verdict')!r}, n_per_arm={assessment.get('n_per_arm')!r}). "
        "Promotion and retirement are evaluated on opposite evidence by design -- 'improve "
        "or hold' would let a no-effect cue satisfy both."
    )


def _retire_rejection_message(exit_evidence: str, assessment: dict[str, Any]) -> str:
    return (
        f"Assessment {exit_evidence!r} does not support retirement: retire_cue requires "
        f"verdict == 'no-effect' and n_per_arm >= {gates.MIN_N_PER_ARM} (got "
        f"verdict={assessment.get('verdict')!r}, n_per_arm={assessment.get('n_per_arm')!r}). "
        "An 'inconclusive' result keeps the cue -- an underpowered comparison is not "
        "evidence of absence."
    )


def promote_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    cue_id: str,
    entry_evidence: str,
    *,
    max_active_cues: int,
    working_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    cue = _find_cue(doc, cue_id)
    if cue is None:
        raise CueStateError(f"No cue {cue_id!r} found for {provider}/{model}/{domain}.")
    if cue.get("unsupported"):
        raise CueStateError(
            f"Cue {cue_id!r} is unsupported: its entire origin observation set was removed "
            "by a prior forget() and can no longer be resolved. Propose a new cue with "
            "fresh origin evidence instead of promoting this one."
        )

    assessment = load_assessment(root, entry_evidence)
    if not gates.promote(assessment):
        raise CueStateError(_promote_rejection_message(entry_evidence, assessment))

    if _count_active(doc) >= max_active_cues:
        raise CueBudgetError(
            f"Promoting {cue_id!r} would exceed max_active_cues ({max_active_cues}); active "
            "cue count is already at the ceiling. Retire or shadow an existing cue first -- "
            "promotion has no other ceiling."
        )

    cue["status"] = "active"
    cue["entry_evidence"] = entry_evidence
    cue["dosed_at"] = cue.get("dosed_at") or "session-start"

    if dry_run:
        return dict(cue)

    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"promote {cue_id}: entry_evidence={entry_evidence}",
    )
    return dict(cue)


def _reject_if_human_origin(cue: dict[str, Any], cue_id: str, action: str) -> None:
    """A human-authored cue is never removed by the automated pipeline.

    `origin_class: human` marks a cue a person put there deliberately. The
    evidence machinery has no standing to take it away: its counters measure
    the automated loop, and a probe suite cannot see the rare-but-costly event
    a human cue usually exists to prevent. Enforced here rather than left to
    agent instructions, because agent-layer policy is advisory and the tool is
    the enforcement point.

    A person can still remove it -- `mute_cue` is the human override and does
    not pass through this guard.
    """
    if cue.get("origin_class") == "human":
        raise CueStateError(
            f"Cue {cue_id!r} has origin_class 'human' and cannot be {action} by the "
            "automated pipeline. A human authored it deliberately; only a human removes "
            "it. Use mute_cue for an explicit human override."
        )


def retire_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    cue_id: str,
    exit_evidence: str,
    *,
    working_dir: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    cue = _find_cue(doc, cue_id)
    if cue is None:
        raise CueStateError(f"No cue {cue_id!r} found for {provider}/{model}/{domain}.")
    _reject_if_human_origin(cue, cue_id, "retired")
    if cue.get("status") != "shadowed":
        raise CueStateError(
            f"Cue {cue_id!r} is {cue.get('status')!r}, not 'shadowed'. Retirement goes "
            "through shadowed only -- shadow the cue first and let the shadow window close."
        )

    assessment = load_assessment(root, exit_evidence)
    if not gates.retire(assessment):
        raise CueStateError(_retire_rejection_message(exit_evidence, assessment))

    cue["status"] = "faded"
    cue["exit_evidence"] = exit_evidence

    if dry_run:
        return dict(cue)

    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"retire {cue_id}: exit_evidence={exit_evidence}",
    )
    return dict(cue)


def shadow_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    cue_id: str,
    *,
    cfg: dict[str, Any],
    working_dir: Path | None = None,
) -> dict[str, Any]:
    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    cue = _find_cue(doc, cue_id)
    if cue is None:
        raise CueStateError(f"No cue {cue_id!r} found for {provider}/{model}/{domain}.")
    _reject_if_human_origin(cue, cue_id, "shadowed")
    if cue.get("status") != "active":
        raise CueStateError(
            f"Cue {cue_id!r} is {cue.get('status')!r}, not 'active'. Only an active cue "
            "can be shadowed."
        )
    cue["status"] = "shadowed"
    cue["shadowed_at"] = _utcnow()
    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"shadow {cue_id}",
    )
    update_state(root, cfg, fade_attempts_delta=1)
    return dict(cue)


def restore_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    cue_id: str,
    *,
    cfg: dict[str, Any] | None = None,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    cue = _find_cue(doc, cue_id)
    if cue is None:
        raise CueStateError(f"No cue {cue_id!r} found for {provider}/{model}/{domain}.")
    if cue.get("status") not in ("shadowed", "faded"):
        raise CueStateError(
            f"Cue {cue_id!r} is {cue.get('status')!r}; restore_cue only applies from "
            "'shadowed' or 'faded'."
        )
    cue["status"] = "active"
    cue["pinned"] = True
    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"restore {cue_id} (pinned)",
    )
    update_state(root, cfg or {}, shadow_restores_delta=1)
    return dict(cue)


def pin_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    cue_id: str,
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """Human override: pin a cue so it is not treated as an auto-fade candidate."""
    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    cue = _find_cue(doc, cue_id)
    if cue is None:
        raise CueStateError(f"No cue {cue_id!r} found for {provider}/{model}/{domain}.")
    cue["pinned"] = True
    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"pin {cue_id}",
    )
    return dict(cue)


def mute_cue(
    root: Path,
    provider: str,
    model: str,
    domain: str,
    cue_id: str,
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """Human override: force a cue out of dosing immediately.

    Unlike shadow_cue (the evidence-informed lifecycle step), mute_cue does
    not touch fade_attempts/shadow_restores -- those counters specifically
    measure the automated shadow/restore pipeline's trustworthiness, not an
    out-of-band human action.
    """
    doc = read_ledger_doc(root, provider, model, domain, working_dir=working_dir)
    cue = _find_cue(doc, cue_id)
    if cue is None:
        raise CueStateError(f"No cue {cue_id!r} found for {provider}/{model}/{domain}.")
    if cue.get("status") == "faded":
        raise CueStateError(f"Cue {cue_id!r} is already faded; nothing to mute.")
    cue["status"] = "shadowed"
    cue["shadowed_at"] = _utcnow()
    _stamp_and_write(
        root,
        provider,
        model,
        domain,
        doc,
        working_dir=working_dir,
        commit_message=f"mute {cue_id}",
    )
    return dict(cue)


def log_assessment(
    root: Path,
    run: str,
    provider: str,
    model: str,
    domain: str,
    probes: list[str],
    verdict: str,
    n_per_arm: int,
    mean: float,
    variance: float,
) -> dict[str, Any]:
    if verdict not in _VALID_VERDICTS:
        raise LedgerError(f"verdict must be one of {_VALID_VERDICTS}, got {verdict!r}.")
    if not isinstance(probes, list) or not probes:
        raise LedgerError("probes must be a non-empty list of probe identifiers.")
    try:
        n_per_arm_int = int(n_per_arm)
    except (TypeError, ValueError) as exc:
        raise LedgerError(f"n_per_arm must be an integer, got {n_per_arm!r}.") from exc
    if n_per_arm_int < 0:
        raise LedgerError("n_per_arm must not be negative.")

    record = {
        "v": 1,
        "run": run,
        "provider": provider,
        "model": model,
        "domain": domain,
        "probes": list(probes),
        "verdict": verdict,
        "n_per_arm": n_per_arm_int,
        "mean": float(mean),
        "variance": float(variance),
        "created": _utcnow(),
    }
    path = assessment_path(root, run)
    with _locked(path):
        _atomic_write_text(path, json.dumps(record, indent=2, sort_keys=False) + "\n")
        _git_commit(
            root,
            [path],
            f"assessment {run}: verdict={verdict} n_per_arm={n_per_arm_int}",
        )
    return record


def forget(root: Path, since: str) -> dict[str, Any]:
    """Delete observation records timestamped on/after `since`, and mark any cue
    whose entire origin set was removed as unsupported (blocks promotion)."""
    since_dt = _parse_timestamp(since)
    obs_dir = observations_dir(root)
    deleted_count = 0
    surviving_ids: set[str] = set()

    if obs_dir.is_dir():
        for file in sorted(obs_dir.glob("*.jsonl")):
            kept: list[dict[str, Any]] = []
            for record in _iter_jsonl(file):
                ts = record.get("ts")
                record_dt = _parse_timestamp(ts) if ts else None
                if record_dt is not None and record_dt >= since_dt:
                    deleted_count += 1
                    continue
                kept.append(record)
                obs_id = record.get("id")
                if obs_id:
                    surviving_ids.add(obs_id)
            with _locked(file):
                if kept:
                    text = (
                        "\n".join(json.dumps(r, sort_keys=False) for r in kept) + "\n"
                    )
                    _atomic_write_text(file, text)
                else:
                    with contextlib.suppress(OSError):
                        file.unlink()

    unsupported_cues: list[str] = []
    root_ledger = ledger_dir(root)
    if root_ledger.is_dir():
        for doc_path in sorted(root_ledger.rglob("*.yaml")):
            with open(doc_path, encoding="utf-8") as f:
                doc = yaml.safe_load(f) or {}
            changed = False
            for cue in doc.get("cues", []):
                origin = cue.get("origin") or []
                if (
                    origin
                    and not any(o in surviving_ids for o in origin)
                    and not cue.get("unsupported")
                ):
                    cue["unsupported"] = True
                    changed = True
                    unsupported_cues.append(cue.get("id", "?"))
            if changed:
                doc["version"] = int(doc.get("version", 0)) + 1
                with _locked(doc_path):
                    _atomic_write_text(doc_path, yaml.safe_dump(doc, sort_keys=False))
                    _git_commit(
                        root,
                        [doc_path],
                        f"ledger: mark unsupported after forget(since={since})",
                    )

    return {"deleted_observations": deleted_count, "unsupported_cues": unsupported_cues}

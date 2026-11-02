"""Counter corruption must fail before shadow/restore mutate temporary cues."""

from __future__ import annotations

import json

import pytest
import yaml
from amplifier_module_tool_preceptor import gates, ledger


def _state():
    return {
        "v": 1,
        "detector_calibrated": True,
        "fade_attempts": 40,
        "shadow_restores": 0,
        "false_fade_rate": 0.0,
    }


def _cue(root, operation):
    observation = ledger.observation_path(root, "fixture")
    observation.parent.mkdir(parents=True)
    observation.write_text(json.dumps({"id": "obs-1"}) + "\n", encoding="utf-8")
    cue = ledger.propose_cue(
        root,
        "a",
        "m",
        "d",
        "Run the tests.",
        ["obs-1"],
        max_cue_chars=200,
        max_active_cues=8,
    )
    ledger.log_assessment(
        root, "positive", "a", "m", "d", ["p"], "positive", 5, 0.4, 0.01
    )
    ledger.promote_cue(root, "a", "m", "d", cue["id"], "positive", max_active_cues=8)
    if operation == "restore":
        ledger.shadow_cue(root, "a", "m", "d", cue["id"], cfg={})
    return cue["id"]


def _transition(root, operation, cue_id):
    fn = ledger.shadow_cue if operation == "shadow" else ledger.restore_cue
    return fn(root, "a", "m", "d", cue_id, cfg={"autonomous": True})


@pytest.mark.parametrize("operation", ["shadow", "restore"])
@pytest.mark.parametrize("field", ["fade_attempts", "shadow_restores"])
@pytest.mark.parametrize(
    "value",
    ["40", None, -1, 40.0, 40.5, True, False, float("nan"), float("inf"), [], {}],
)
def test_invalid_counters_reject_before_any_cue_or_state_write(
    tmp_path, operation, field, value
):
    cue_id = _cue(tmp_path, operation)
    ledger.write_state(tmp_path, {**_state(), field: value})
    cue_path = ledger.ledger_doc_path(tmp_path, "a", "m", "d")
    state_path = ledger.state_path(tmp_path)
    before_cue, before_state = cue_path.read_bytes(), state_path.read_bytes()
    with pytest.raises(ledger.LedgerError, match=field):
        _transition(tmp_path, operation, cue_id)
    assert cue_path.read_bytes() == before_cue
    assert state_path.read_bytes() == before_state


@pytest.mark.parametrize("operation", ["shadow", "restore"])
@pytest.mark.parametrize("field", ["fade_attempts", "shadow_restores"])
def test_missing_counters_reject_before_mutation(tmp_path, operation, field):
    cue_id = _cue(tmp_path, operation)
    state = _state()
    state.pop(field)
    ledger.write_state(tmp_path, state)
    cue_path = ledger.ledger_doc_path(tmp_path, "a", "m", "d")
    state_path = ledger.state_path(tmp_path)
    before_cue, before_state = cue_path.read_bytes(), state_path.read_bytes()
    with pytest.raises(ledger.LedgerError, match=field):
        _transition(tmp_path, operation, cue_id)
    assert cue_path.read_bytes() == before_cue
    assert state_path.read_bytes() == before_state


@pytest.mark.parametrize("operation", ["shadow", "restore"])
@pytest.mark.parametrize("raw", ["{", "[]", "null", '{"v": 2}'])
def test_unreadable_history_rejects_before_mutation(tmp_path, operation, raw):
    cue_id = _cue(tmp_path, operation)
    ledger.state_path(tmp_path).write_text(raw, encoding="utf-8")
    cue_path = ledger.ledger_doc_path(tmp_path, "a", "m", "d")
    before_cue = cue_path.read_bytes()
    with pytest.raises(ledger.LedgerError):
        _transition(tmp_path, operation, cue_id)
    assert cue_path.read_bytes() == before_cue
    assert ledger.state_path(tmp_path).read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("operation", ["shadow", "restore"])
def test_invalid_counters_reject_before_fingerprint_reconciliation(tmp_path, operation):
    cue_id = _cue(tmp_path, operation)
    ledger.write_state(tmp_path, {**_state(), "fade_attempts": "40"})
    cue_path = ledger.ledger_doc_path(tmp_path, "a", "m", "d")
    doc = yaml.safe_load(cue_path.read_text(encoding="utf-8"))
    doc["model_fingerprint"] = "sha256:stale"
    cue_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    before = cue_path.read_bytes()
    with pytest.raises(ledger.LedgerError, match="fade_attempts"):
        _transition(tmp_path, operation, cue_id)
    assert cue_path.read_bytes() == before


@pytest.mark.parametrize("operation", ["shadow", "restore"])
@pytest.mark.parametrize("rate", [0.0, "NaN", None])
def test_safe_and_human_transitions_keep_valid_counter_recording(
    tmp_path, operation, rate
):
    cue_id = _cue(tmp_path, operation)
    state = _state()
    if rate is None:
        state.pop("false_fade_rate")
    else:
        state["false_fade_rate"] = rate
    ledger.write_state(tmp_path, state)
    result = _transition(tmp_path, operation, cue_id)
    assert result["status"] == ("shadowed" if operation == "shadow" else "active")
    if operation == "restore":
        assert result["pinned"] is True
    recorded = ledger.read_state(tmp_path)
    assert recorded["fade_attempts"] == 40 + (operation == "shadow")
    assert recorded["shadow_restores"] == (operation == "restore")
    assert recorded["false_fade_rate"] == gates.compute_false_fade_rate(
        recorded["shadow_restores"], recorded["fade_attempts"]
    )
    assert recorded["autonomy_unlocked"] is True

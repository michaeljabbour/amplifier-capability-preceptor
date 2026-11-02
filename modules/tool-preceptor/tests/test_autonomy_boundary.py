"""Untrusted configuration and persisted measurements must never grant authority.

Every filesystem fixture is under pytest's temporary directory. Valid controls
exercise the same native promote/retire paths as corrupted-state regressions.
"""

from __future__ import annotations

import json

import pytest
from amplifier_module_tool_preceptor import PreceptorTool, gates, ledger


def _state():
    return {
        "v": 1,
        "detector_calibrated": True,
        "fade_attempts": 40,
        "shadow_restores": 0,
        "false_fade_rate": 0.0,
    }


def _cfg():
    return {"autonomous": True, "min_fade_attempts": 40, "false_fade_ceiling": 0.1}


_INVALID_BOOLEANS = ["false", "true", "", 0, 1, None, [], {}, False]
_INVALID_RATES = [
    "NaN",
    "0",
    "0.05",
    None,
    float("nan"),
    float("inf"),
    float("-inf"),
    -0.01,
    1.01,
    False,
    True,
    [],
    {},
]
_INVALID_COUNTERS = [
    "40",
    "0",
    None,
    -1,
    40.5,
    40.0,
    False,
    True,
    float("nan"),
    float("inf"),
    [],
    {},
]


@pytest.mark.parametrize("value", _INVALID_BOOLEANS)
@pytest.mark.parametrize("field", ["autonomous", "detector_calibrated"])
def test_authority_requires_real_true_boolean(field, value):
    state, cfg = _state(), _cfg()
    (cfg if field == "autonomous" else state)[field] = value
    unlocked, reason = gates.autonomy_unlocked(state, cfg)
    assert unlocked is False
    assert field in reason


@pytest.mark.parametrize("value", _INVALID_RATES)
@pytest.mark.parametrize("field", ["false_fade_rate", "false_fade_ceiling"])
def test_authority_rejects_nonfinite_out_of_range_or_non_numeric_rates(field, value):
    state, cfg = _state(), _cfg()
    (cfg if field == "false_fade_ceiling" else state)[field] = value
    unlocked, reason = gates.autonomy_unlocked(state, cfg)
    assert unlocked is False
    assert field in reason


@pytest.mark.parametrize("value", _INVALID_COUNTERS)
@pytest.mark.parametrize(
    "field", ["fade_attempts", "shadow_restores", "min_fade_attempts"]
)
def test_authority_requires_integer_counters(field, value):
    state, cfg = _state(), _cfg()
    (cfg if field == "min_fade_attempts" else state)[field] = value
    unlocked, reason = gates.autonomy_unlocked(state, cfg)
    assert unlocked is False
    assert field in reason


@pytest.mark.parametrize(
    "field",
    ["detector_calibrated", "fade_attempts", "shadow_restores", "false_fade_rate"],
)
def test_missing_measured_evidence_stays_locked_after_loading(tmp_path, field):
    state = _state()
    state.pop(field)
    ledger.write_state(tmp_path, state)
    unlocked, reason = gates.autonomy_unlocked(ledger.read_state(tmp_path), _cfg())
    assert unlocked is False
    assert field in reason


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "",
        "null",
        "true",
        "40",
        '"state"',
        "[]",
        '[["detector_calibrated", true], ["fade_attempts", 40]]',
    ],
)
def test_corrupted_or_nonobject_storage_stays_locked(tmp_path, raw):
    ledger.state_path(tmp_path).write_text(raw, encoding="utf-8")
    unlocked, reason = gates.autonomy_unlocked(ledger.read_state(tmp_path), _cfg())
    assert unlocked is False
    assert reason


@pytest.mark.parametrize("version", [None, "1", True, 0, 2])
def test_unknown_or_missing_stored_schema_stays_locked(tmp_path, version):
    state = _state()
    if version is None:
        state.pop("v")
    else:
        state["v"] = version
    ledger.write_state(tmp_path, state)
    assert gates.autonomy_unlocked(ledger.read_state(tmp_path), _cfg())[0] is False


@pytest.mark.parametrize("value", [None, [], 1, "state"])
def test_nonobject_gate_inputs_stay_locked(value):
    assert gates.autonomy_unlocked(value, _cfg())[0] is False
    assert gates.autonomy_unlocked(_state(), value)[0] is False


@pytest.mark.parametrize(
    "updates",
    [
        {"shadow_restores": 41},
        {"shadow_restores": 5, "false_fade_rate": 0.0},
        {"fade_attempts": 0},
    ],
)
def test_impossible_or_underreported_history_stays_locked(updates):
    assert gates.autonomy_unlocked({**_state(), **updates}, _cfg())[0] is False


def test_zero_configured_minimum_cannot_replace_measured_history():
    assert (
        gates.autonomy_unlocked(_state(), {**_cfg(), "min_fade_attempts": 0})[0]
        is False
    )


@pytest.mark.parametrize(
    "updates",
    [
        {},
        {"shadow_restores": 1, "false_fade_rate": 0.025},
        {"fade_attempts": 47, "shadow_restores": 2, "false_fade_rate": 0.0426},
        {"fade_attempts": 41, "shadow_restores": 4, "false_fade_rate": 4 / 41},
    ],
)
def test_normal_and_documented_rounded_measurements_unlock(tmp_path, updates):
    ledger.write_state(tmp_path, {**_state(), **updates})
    assert gates.autonomy_unlocked(ledger.read_state(tmp_path), _cfg())[0] is True


def test_missing_file_initializes_recording_without_granting_autonomy(tmp_path):
    state = ledger.read_state(tmp_path)
    assert state["fade_attempts"] == state["shadow_restores"] == 0
    assert state["false_fade_rate"] == 0
    assert gates.autonomy_unlocked(state, _cfg())[0] is False
    recorded = ledger.update_state(tmp_path, _cfg(), fade_attempts_delta=1)
    assert recorded["fade_attempts"] == 1
    assert recorded["false_fade_rate"] == 0
    assert recorded["autonomy_unlocked"] is False


def test_recording_recomputes_measured_rate_and_relocks(tmp_path):
    ledger.write_state(tmp_path, _state())
    state = ledger.update_state(tmp_path, _cfg(), shadow_restores_delta=1)
    assert state["false_fade_rate"] == 1 / 40
    assert state["autonomy_unlocked"] is True
    state = ledger.update_state(tmp_path, _cfg(), shadow_restores_delta=3)
    assert state["false_fade_rate"] == 0.1
    assert state["autonomy_unlocked"] is False


@pytest.mark.parametrize("field", ["fade_attempts", "shadow_restores"])
@pytest.mark.parametrize("value", _INVALID_COUNTERS)
def test_recording_cannot_coerce_corrupted_counters_into_valid_history(
    tmp_path, field, value
):
    ledger.write_state(tmp_path, {**_state(), field: value})
    before = ledger.state_path(tmp_path).read_bytes()
    with pytest.raises(ledger.LedgerError, match=field):
        ledger.update_state(tmp_path, _cfg(), fade_attempts_delta=1)
    assert ledger.state_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize("value", _INVALID_BOOLEANS + [True])
def test_tool_constructor_does_not_convert_values_into_write_authority(value):
    tool = PreceptorTool(None, {"writable": value, "autonomous": value})
    assert tool.writable is (value is True)
    assert gates.autonomy_unlocked(_state(), tool._autonomy_cfg)[0] is (value is True)


def test_tool_constructor_preserves_locked_defaults():
    tool = PreceptorTool(None, {})
    assert tool.writable is False
    assert gates.autonomy_unlocked(_state(), tool._autonomy_cfg)[0] is False


def test_enabled_autonomy_keeps_documented_numeric_defaults():
    tool = PreceptorTool(None, {"autonomous": True})
    assert tool._autonomy_cfg == _cfg()
    assert gates.autonomy_unlocked(_state(), tool._autonomy_cfg)[0] is True


@pytest.mark.parametrize("value", _INVALID_BOOLEANS + [True])
async def test_native_writes_require_real_true_boolean(tmp_path, value):
    root = tmp_path / "preceptor"
    tool = PreceptorTool(None, {"root": str(root), "writable": value})
    result = await tool.execute(
        {
            "operation": "log_assessment",
            "run": "fixture",
            "provider": "a",
            "model": "m",
            "domain": "d",
            "probes": ["p"],
            "verdict": "positive",
            "n_per_arm": 5,
            "mean": 0.4,
            "variance": 0.01,
        }
    )
    assert result.success is (value is True)
    if value is True:
        assert ledger.assessment_path(root, "fixture").is_file()
    else:
        assert "credentialer" in result.error["message"]
        assert not root.exists()


@pytest.mark.parametrize(
    "field,values",
    [
        ("min_fade_attempts", _INVALID_COUNTERS + [0]),
        ("false_fade_ceiling", _INVALID_RATES),
    ],
)
def test_tool_constructor_cannot_launder_invalid_autonomy_configuration(field, values):
    for value in values:
        tool = PreceptorTool(None, {"autonomous": True, field: value})
        assert gates.autonomy_unlocked(_state(), tool._autonomy_cfg)[0] is False


@pytest.mark.parametrize("operation", ["promote_cue", "retire_cue"])
@pytest.mark.parametrize(
    "corruption",
    ["nan", "negative", "missing", "calibration", "underreported", "valid"],
)
async def test_native_mutation_boundary_keeps_locked_proposals_off_disk(
    tmp_path, operation, corruption
):
    root = tmp_path / "preceptor"
    obs = ledger.observation_path(root, "fixture")
    obs.parent.mkdir(parents=True)
    obs.write_text(
        json.dumps({"id": "obs-1", "session": "fixture"}) + "\n", encoding="utf-8"
    )
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
    ledger.log_assessment(
        root, "no-effect", "a", "m", "d", ["p"], "no-effect", 5, 0.0, 0.01
    )
    if operation == "retire_cue":
        ledger.promote_cue(
            root, "a", "m", "d", cue["id"], "positive", max_active_cues=8
        )
        ledger.shadow_cue(root, "a", "m", "d", cue["id"], cfg={})
    state = _state()
    if corruption == "nan":
        state["false_fade_rate"] = "NaN"
    elif corruption == "negative":
        state["false_fade_rate"] = -0.01
    elif corruption == "missing":
        state.pop("false_fade_rate")
    elif corruption == "calibration":
        state["detector_calibrated"] = "false"
    elif corruption == "underreported":
        state["shadow_restores"] = 5
    ledger.write_state(root, state)
    path = ledger.ledger_doc_path(root, "a", "m", "d")
    before = path.read_bytes()
    tool = PreceptorTool(
        None, {"root": str(root), "writable": True, "autonomous": True}
    )
    evidence = (
        {"entry_evidence": "positive"}
        if operation == "promote_cue"
        else {"exit_evidence": "no-effect"}
    )
    result = await tool.execute(
        {
            "operation": operation,
            "provider": "a",
            "model": "m",
            "domain": "d",
            "cue_id": cue["id"],
            **evidence,
        }
    )
    assert result.success is True
    expected_status = "active" if operation == "promote_cue" else "faded"
    if corruption == "valid":
        assert result.output["status"] == expected_status
        assert (
            ledger.read_ledger_doc(root, "a", "m", "d")["cues"][0]["status"]
            == expected_status
        )
        assert path.read_bytes() != before
    else:
        assert result.output["locked"] is True
        assert result.output["proposed_diff"]["status"] == expected_status
        assert path.read_bytes() == before

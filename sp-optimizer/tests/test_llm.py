"""Offline tests for the decision-step prompt building and reply parsing."""
import json

import pytest

from scripts.llm import _parse_change, build_user_prompt
from scripts.models import AttemptRecord, DecisionContext, PlanScore


def test_parse_change_plain_json():
    c = _parse_change('{"kind": "index", "rationale": "r", "apply_sql": "A", '
                      '"rollback_sql": "B", "target_object": "ix"}')
    assert c.kind == "index" and c.apply_sql == "A"


def test_parse_change_fenced_json():
    raw = '```json\n{"kind": "recompile", "rationale": "", "apply_sql": "X", ' \
          '"rollback_sql": "", "target_object": ""}\n```'
    assert _parse_change(raw).kind == "recompile"


def test_parse_change_json_wrapped_in_prose():
    raw = 'Sure! Here is the change:\n{"kind": "option_hint", "rationale": "r", ' \
          '"apply_sql": "A", "rollback_sql": "B", "target_object": "t"}\nHope that helps.'
    assert _parse_change(raw).kind == "option_hint"


def test_parse_change_raises_on_garbage():
    with pytest.raises(ValueError):
        _parse_change("I could not decide on a change.")


def test_prompt_includes_sandbox_and_attempts():
    scores = [PlanScore(combo_label="c", score=50.0, warnings=["w"])]
    ctx = DecisionContext(
        sandbox_proc="dbo.p_opt_v2",
        attempts=[AttemptRecord(iteration=0, kind="index", target_object="ix_a",
                                outcome="rolled_back", detail="c2 regressed")],
    )
    payload = json.loads(build_user_prompt("CREATE PROC p AS SELECT 1;", scores, ctx))
    assert payload["sandbox_procedure"] == "dbo.p_opt_v2"
    assert payload["previous_attempts"][0]["outcome"] == "rolled_back"


def test_prompt_omits_optional_keys_when_absent():
    scores = [PlanScore(combo_label="c", score=50.0)]
    payload = json.loads(build_user_prompt("p", scores))
    assert "sandbox_procedure" not in payload
    assert "previous_attempts" not in payload
    assert "query_store_plans" not in payload

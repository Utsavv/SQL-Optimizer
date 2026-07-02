"""Offline tests for the shared scoring helpers."""
from scripts.models import ParamCombo, PlanScore, fraction_good, workload_score


def _score(label: str, score: float) -> PlanScore:
    return PlanScore(combo_label=label, score=score)


def test_fraction_good_unweighted_without_combos():
    scores = [_score("a", 80), _score("b", 40)]
    assert fraction_good(scores, 75.0) == 0.5


def test_fraction_good_weights_by_combo_weight():
    # The hot path (weight 3) is good, the rare edge case (weight 1) is bad:
    # weighted, 75% of representative traffic is good — not a 50/50 split.
    combos = [ParamCombo(values={}, label="hot", weight=3.0),
              ParamCombo(values={}, label="rare", weight=1.0)]
    scores = [_score("hot", 90), _score("rare", 10)]
    assert fraction_good(scores, 75.0, combos) == 0.75
    # and inverted: only the rare case is good -> 25%
    scores = [_score("hot", 10), _score("rare", 90)]
    assert fraction_good(scores, 75.0, combos) == 0.25


def test_fraction_good_falls_back_when_labels_unknown():
    combos = [ParamCombo(values={}, label="x", weight=2.0)]
    scores = [_score("unrelated", 80), _score("other", 40)]
    # unknown labels default to weight 1.0 -> same as unweighted
    assert fraction_good(scores, 75.0, combos) == 0.5


def test_workload_score_weighted_average():
    combos = [ParamCombo(values={}, label="a", weight=3.0),
              ParamCombo(values={}, label="b", weight=1.0)]
    scores = [_score("a", 100), _score("b", 0)]
    assert workload_score(scores, combos) == 75.0

"""Unit tests for timetrace.agent — pure parts only, no DB.

Covers question parsing and the deterministic verdict tiers with synthetic
inputs. The clarification paths of answer() are tested without a database
(they return before any graph call).
"""

import pytest

from timetrace import agent as A


# ---------------------------------------------------------------------------
# parse_question
# ---------------------------------------------------------------------------


def test_parse_quoted_target_wins():
    p = A.parse_question('is it safe to change "rich/console.py"?')
    assert p["target_text"] == "rich/console.py"
    assert p["t"] is None


def test_parse_bare_path():
    p = A.parse_question("who calls rich/console.py these days")
    assert p["target_text"] == "rich/console.py"


def test_parse_dotted_qualname():
    p = A.parse_question("is Server.start risky to change")
    assert p["target_text"] == "Server.start"


def test_parse_skips_abbreviations():
    p = A.parse_question("e.g. is it safe")
    assert p["target_text"] is None


def test_parse_unparseable_gives_none():
    for q in ["hello", "what is the weather", ""]:
        assert A.parse_question(q)["target_text"] is None


def test_parse_as_of_date():
    p = A.parse_question('who called "a.py" as of 2024-03-01')
    assert p["target_text"] == "a.py"
    assert p["t"] == 1709251200  # 2024-03-01T00:00:00Z


def test_parse_as_of_epoch():
    p = A.parse_question("blast radius of f.py as of 1709251200")
    assert p["t"] == 1709251200


# ---------------------------------------------------------------------------
# verdict tiers
# ---------------------------------------------------------------------------


def test_risk_score_formula():
    assert A.risk_score(0, 0, 0, 0) == 0
    assert A.risk_score(2, 1, 5, 2) == 2 * 2 + 3 * 1 + 5 + 2 * 2 == 16


@pytest.mark.parametrize("score,expected", [
    (0, "SAFE TO CHANGE"),
    (6, "SAFE TO CHANGE"),      # boundary: inclusive
    (7, "CHANGE WITH CARE"),
    (15, "CHANGE WITH CARE"),   # boundary: inclusive
    (16, "RISKY"),
    (100, "RISKY"),
])
def test_verdict_tier_boundaries(score, expected):
    assert A.verdict_tier(score) == expected


def test_quiet_utility_is_safe_and_hot_module_is_risky():
    assert A.verdict_tier(A.risk_score(1, 0, 1, 1)) == "SAFE TO CHANGE"
    assert A.verdict_tier(A.risk_score(6, 3, 9, 4)) == "RISKY"


# ---------------------------------------------------------------------------
# answer() clarification paths (no DB touched)
# ---------------------------------------------------------------------------


def test_answer_asks_for_clarification_when_unparseable():
    out = A.answer("tell me something interesting")
    assert out["verdict"] == "NEED MORE INFO"
    assert "which module or function" in out["summary"]
    assert out["proof_chain"] == []
    assert out["meta"]["queries_run"] == []  # no graph call happened


def test_answer_result_shape_keys():
    out = A.answer("???")
    assert set(out) == {"verdict", "summary", "proof_chain", "meta"}
    assert set(out["meta"]) == {"target_id", "t", "repo", "queries_run"}

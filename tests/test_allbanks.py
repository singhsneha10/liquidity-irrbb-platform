"""T6 all-banks outflows: hand-worked cases, written before the real data is computed."""

import pandas as pd
import pytest

from riskplatform.model_validation.allbanks import (
    event_quarters,
    largest,
    quarterly_outflows,
    size_of,
    summarise,
)

Q = pd.to_datetime(["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"])


def _deposits(rows):
    return pd.DataFrame(rows, columns=["cert", "quarter", "deposits_thousands"])


def _events(rows):
    frame = pd.DataFrame(rows, columns=["event_id", "cert", "acq_cert", "out_cert", "effective",
                                        "code"])  # fmt: skip
    for c in ("cert", "acq_cert", "out_cert"):
        frame[c] = frame[c].astype("Int64")
    frame["effective"] = pd.to_datetime(frame["effective"])
    return frame


NO_EVENTS = _events([])


def test_q_and_the_monthly_lower_bound_by_hand():
    """1,000,000 -> 729,000 is a 27.1% quarterly fall; then at least one month fell by 10%,
    because 0.9 ** 3 = 0.729. A rise counts as no outflow."""
    table = quarterly_outflows(
        _deposits([(1, Q[0], 1_000_000), (1, Q[1], 729_000), (1, Q[2], 800_000)]), NO_EVENTS, 10_000
    )
    second, third = table.iloc[1], table.iloc[2]
    assert second["q"] == pytest.approx(0.271) and second["m"] == pytest.approx(0.10)
    assert third["q"] == 0.0 and third["m"] == 0.0
    assert table["excluded"].tolist() == ["first", "", ""]


def test_events_mark_the_quarter_they_fall_in_for_every_bank_they_touch():
    events = _events([("e1", 2, 9, 2, "2023-04-01", 223), ("e2", 5, None, None, "2023-06-30", 722)])
    pairs = {(int(c), q) for c, q in event_quarters(events).itertuples(index=False)}
    assert pairs == {(2, Q[1]), (9, Q[1]), (5, Q[1])}  # 1 Apr and 30 Jun: both in Q2


def test_each_exclusion_reason():
    deposits = _deposits([
        (1, Q[0], 100_000), (1, Q[2], 50_000),          # Q2 missing: a gap
        (2, Q[0], 100_000), (2, Q[1], 50_000),          # a merger in Q2
        (3, Q[0], 5_000), (3, Q[1], 1_000),             # under $10mn before
        (4, Q[0], 100_000), (4, Q[1], 50_000),          # named by the hand check
        (5, Q[0], 100_000), (5, Q[1], 50_000),          # kept: a real 50% fall
    ])  # fmt: skip
    events = _events([("e1", 2, 9, 2, "2023-05-15", 223)])
    table = quarterly_outflows(deposits, events, 10_000, hand_excluded={(4, Q[1])})
    reasons = dict(zip(zip(table["cert"], table["quarter"]), table["excluded"], strict=True))
    assert reasons[(1, Q[2])] == "gap"
    assert reasons[(2, Q[1])] == "structure_event"
    assert reasons[(3, Q[1])] == "small"
    assert reasons[(4, Q[1])] == "hand_check"
    assert reasons[(5, Q[1])] == ""


def test_sizes_and_the_planned_statistics():
    assert size_of(pd.Series([999_999, 1_000_000, 10_000_000])).tolist() == [
        "under $1bn", "$1bn to $10bn", "over $10bn"]  # fmt: skip
    deposits = _deposits(
        [(c, q, d) for c, d in ((1, 2_000_000), (2, 20_000_000)) for q in Q[:2] for d in [d]]
        + [(3, Q[0], 2_000_000), (3, Q[1], 1_458_000)]  # 27.1% fall: m = 10%, a $1bn-$10bn bank
    )
    table = quarterly_outflows(deposits, NO_EVENTS, 10_000)
    stats = summarise(table, (0.05, 0.10), stress_years=[2023])
    assert stats["kept"] == 3 and stats["excluded"] == {"first": 3}
    mid = stats["$1bn to $10bn"]["all years"]
    assert mid["bank_quarters"] == 2 and mid["share_above_stable"] == 0.5
    assert mid["share_above_less_stable"] == 0.0  # exactly 10% is not above 10%
    assert stats["all banks"]["stress years"]["bank_quarters"] == 3
    assert stats["all banks"]["other years"]["bank_quarters"] == 0


def test_largest_lists_only_kept_big_banks():
    deposits = _deposits([(1, Q[0], 5_000_000), (1, Q[1], 2_500_000),
                          (2, Q[0], 500_000), (2, Q[1], 100_000)])  # fmt: skip
    top = largest(quarterly_outflows(deposits, NO_EVENTS, 10_000), 20, 1_000_000)
    assert top["cert"].tolist() == [1]  # bank 2 fell more, but is under $1bn


def test_quarter_before_also_marks_the_previous_quarter():
    """Deposits often move the quarter before a merger's legal date: the supplementary filter."""
    events = _events([("e1", 2, 9, 2, "2023-07-01", 223)])  # one day after Q2
    planned = {(int(c), q) for c, q in event_quarters(events).itertuples(index=False)}
    stricter = {(int(c), q) for c, q in event_quarters(events, 1).itertuples(index=False)}
    assert planned == {(2, Q[2]), (9, Q[2])}
    assert stricter == planned | {(2, Q[1]), (9, Q[1])}
    deposits = _deposits([(2, Q[0], 100_000), (2, Q[1], 1_000)])  # gone the quarter before
    assert quarterly_outflows(deposits, events, 10_000)["excluded"].iloc[1] == ""
    assert quarterly_outflows(deposits, events, 10_000, quarters_before=1)["excluded"].iloc[
        1] == "structure_event"  # fmt: skip


def test_hand_check_config_is_complete_and_only_misses_are_excluded():
    from pathlib import Path

    from riskplatform.config import load_validation_config

    cfg = load_validation_config(Path(__file__).resolve().parents[1] / "config" / "validation.yaml")
    checks = cfg.all_banks.hand_check
    assert len(checks) == cfg.all_banks.top_n
    assert len({(h.cert, h.quarter) for h in checks}) == len(checks)  # no duplicates
    misses = {(h.cert, h.quarter) for h in checks if h.verdict == "missed_structure_event"}
    assert cfg.all_banks.excluded_by_hand == misses
    assert all(
        "FDIC history" in h.evidence for h in checks if h.verdict == "missed_structure_event"
    )

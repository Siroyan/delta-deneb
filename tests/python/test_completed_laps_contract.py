from decimal import Decimal
import json
from pathlib import Path

import pytest

from backend.telemetry.model import InvalidSample, lap_summaries, normalize_sample


FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/mqtt-completed-laps.json").read_text())


def normalized(**changes):
    return normalize_sample(FIXTURE | changes, received_at_ms=1800000000000)


def test_a_single_latest_message_contains_all_device_confirmed_laps():
    sample = normalized()
    assert sample["completed_lap_times_ms"] == [362000, 358000]
    assert lap_summaries([sample]) == [
        {"lap": 1, "time_ms": 362000, "source": "device"},
        {"lap": 2, "time_ms": 358000, "source": "device"},
    ]
    assert "memo" not in sample


@pytest.mark.parametrize("lap,total,current,times", [
    (0, 0, 0, []),
    (1, 0, 0, []),
    (1, 1000, 1000, []),
    (3, 765000, 45000, [362000, 358000, 45000]),
    (2, 362000, 0, [362000, 0]),
    (100, 100000, 1000, [1000] * 99),
    (100, 100000, 1000, [1000] * 100),
])
def test_waiting_start_finished_zero_final_lap_and_one_hundred_laps(lap, total, current, times):
    sample = normalized(lap_number=lap, total_time_ms=total, lap_time_ms=current, completed_lap_times_ms=times)
    assert sample["completed_lap_times_ms"] == times
    assert [lap["time_ms"] for lap in lap_summaries([sample])] == times


def test_dynamodb_decimal_lists_are_serializable_in_order():
    sample = normalized(completed_lap_times_ms=[Decimal(362000), Decimal(358000)])
    assert json.loads(json.dumps(sample))["completed_lap_times_ms"] == [362000, 358000]


@pytest.mark.parametrize("changes", [
    {"completed_lap_times_ms": "[362000,358000]"},
    {"completed_lap_times_ms": {}},
    {"completed_lap_times_ms": [362000, True]},
    {"completed_lap_times_ms": [362000, -1]},
    {"completed_lap_times_ms": [362000, 358000.5]},
    {"completed_lap_times_ms": [362000, Decimal("NaN")]},
    {"completed_lap_times_ms": [362000, 2**53]},
    {"completed_lap_times_ms": [1000] * 101},
    {"completed_lap_times_ms": [362000]},
    {"completed_lap_times_ms": [362000, 358001]},
    {"completed_lap_times_ms": [362000, 358000, 40000]},
    {"lap_number": 101},
    {"lap_number": 0, "total_time_ms": 1, "lap_time_ms": 0, "completed_lap_times_ms": None},
    {"lap_number": 0, "total_time_ms": 0, "lap_time_ms": 0, "completed_lap_times_ms": [0]},
])
def test_bad_arrays_or_inconsistent_counters_are_not_presented_as_confirmed(changes):
    with pytest.raises(InvalidSample):
        normalized(**changes)


def test_missing_array_in_old_records_stays_unavailable_instead_of_becoming_empty():
    payload = dict(FIXTURE)
    payload.pop("completed_lap_times_ms")
    sample = normalize_sample(payload, received_at_ms=1800000000000)
    assert sample["completed_lap_times_ms"] is None
    assert lap_summaries([sample]) == []


def test_cancellation_and_new_start_never_reuse_an_older_snapshot_array():
    old = normalized()
    cancelled = normalized(lap_number=0, total_time_ms=0, lap_time_ms=0, completed_lap_times_ms=[])
    restarted = normalized(lap_number=1, total_time_ms=1000, lap_time_ms=1000, completed_lap_times_ms=[])
    assert lap_summaries([old, cancelled]) == []
    assert lap_summaries([old, cancelled, restarted]) == []

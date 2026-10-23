from decimal import Decimal
import json

import pytest

from backend.telemetry.model import InvalidSample, current_run, lap_summaries, normalize_sample


def sample(*, lap=1, total=1000, lap_time=1000, uptime=121000, received=1800000000000):
    return normalize_sample({
        "speed": Decimal("27.125"), "average_speed": Decimal("24.5"),
        "lap_number": Decimal(lap), "total_time_ms": Decimal(total),
        "lap_time_ms": Decimal(lap_time), "timestamp_ms": Decimal(uptime),
        "latitude": Decimal("36.1"), "longitude": Decimal("140.2"),
        "machine_id": "pi", "memo": "private note", "unrelated_secret": "must-not-appear",
    }, received_at_ms=received)


def test_decimals_become_json_numbers_and_device_time_is_never_used_as_utc():
    result = sample()
    assert result["speed"] == 27.125
    assert result["received_at_ms"] == 1800000000000
    assert result["timestamp_ms"] == 121000
    assert "private note" not in json.dumps(result, allow_nan=False)
    assert "must-not-appear" not in json.dumps(result)


@pytest.mark.parametrize("bad", [None, "1", True, -1, Decimal("NaN"), Decimal("Infinity"), 1.5, 2**53])
def test_invalid_counters_cannot_be_returned_as_samples(bad):
    payload = sample()
    payload["total_time_ms"] = bad
    with pytest.raises(InvalidSample):
        normalize_sample(payload, received_at_ms=1800000000000)


def test_missing_gps_pair_and_invalid_speeds_are_null_instead_of_zero():
    payload = sample()
    payload.update(latitude=None, speed=-1, average_speed=float("inf"))
    result = normalize_sample(payload, received_at_ms=1800000000000)
    assert result["latitude"] is None and result["longitude"] is None
    assert result["speed"] is None and result["average_speed"] is None


def test_race_restart_discards_previous_run():
    samples = [sample(total=6000, lap_time=6000), sample(total=1000, lap_time=1000, uptime=126000)]
    assert current_run(samples) == samples[1:]


def test_device_reboot_discards_previous_run_even_when_race_timers_increase():
    samples = [sample(), sample(total=2000, lap_time=2000, uptime=3000)]
    assert current_run(samples) == samples[1:]


def test_completed_lap_time_comes_from_next_lap_boundary():
    samples = [
        sample(total=299500, lap_time=299500),
        sample(lap=2, total=300500, lap_time=500),
        sample(lap=3, total=630500, lap_time=500),
    ]
    assert lap_summaries(samples) == [
        {"lap": 1, "time_ms": 300000, "source": "inferred_transition"},
        {"lap": 2, "time_ms": 330000, "source": "inferred_transition"},
    ]


def test_first_observed_lap_start_is_derived_even_when_lap_one_is_not_loaded():
    samples = [
        sample(lap=2, total=301000, lap_time=1000),
        sample(lap=3, total=631000, lap_time=1000),
    ]
    assert lap_summaries(samples) == [
        {"lap": 2, "time_ms": 330000, "source": "inferred_transition"},
    ]


def test_skipped_lap_never_fabricates_duration():
    samples = [
        sample(lap=3, total=900000, lap_time=20000),
        sample(lap=5, total=1500000, lap_time=20000),
        sample(lap=6, total=1800000, lap_time=20000),
    ]
    assert [lap["time_ms"] for lap in lap_summaries(samples)] == [None, 300000]


def test_last_lap_is_not_called_finished_without_an_observed_transition():
    assert lap_summaries([sample(lap=7, total=2100000, lap_time=300000)]) == []

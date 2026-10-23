"""Normalize delta-vega MQTT fields without exposing arbitrary stored attributes."""

from decimal import Decimal
import math


MAX_SAFE_INTEGER = 2**53 - 1
MAX_LAPS = 100


class InvalidSample(ValueError):
    """A stored item is not a usable delta-vega sample."""


def number(value, *, minimum=None, maximum=None, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    if not math.isfinite(value):
        return None
    if integer and value != int(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return int(value) if integer else float(value)


def normalize_sample(payload, *, received_at_ms):
    """Keep only known fields; the receipt timestamp must come from AWS storage."""
    if not isinstance(payload, dict):
        raise InvalidSample("Expected a telemetry object")
    receipt = number(received_at_ms, integer=True, minimum=0, maximum=MAX_SAFE_INTEGER)
    if receipt is None:
        raise InvalidSample("Missing or invalid AWS receipt timestamp")
    sample = {"received_at_ms": receipt}
    for name in ("timestamp_ms", "total_time_ms", "lap_time_ms", "lap_number"):
        sample[name] = number(payload.get(name), integer=True, minimum=0, maximum=MAX_LAPS if name == "lap_number" else MAX_SAFE_INTEGER)
        if sample[name] is None:
            raise InvalidSample("Missing or invalid telemetry counters")
    if sample["lap_time_ms"] > sample["total_time_ms"]:
        raise InvalidSample("Lap time exceeds total time")
    if sample["lap_number"] == 0 and (sample["total_time_ms"] != 0 or sample["lap_time_ms"] != 0):
        raise InvalidSample("Waiting counters must be zero")
    times = payload.get("completed_lap_times_ms")
    sample["completed_lap_times_ms"] = None
    if times is not None:
        if not isinstance(times, list) or len(times) > MAX_LAPS:
            raise InvalidSample("Invalid completed lap array")
        times = [number(value, integer=True, minimum=0, maximum=MAX_SAFE_INTEGER) for value in times]
        if any(value is None for value in times):
            raise InvalidSample("Invalid completed lap duration")
        lap, total, current = sample["lap_number"], sample["total_time_ms"], sample["lap_time_ms"]
        if lap == 0:
            valid = not times and total == current == 0
        elif len(times) == lap - 1:
            valid = sum(times) + current == total
        elif len(times) == lap:
            valid = sum(times) == total and times[-1] == current
        else:
            valid = False
        if not valid:
            raise InvalidSample("Completed laps disagree with telemetry counters")
        sample["completed_lap_times_ms"] = times
    machine = payload.get("machine_id")
    if not isinstance(machine, str) or not machine or len(machine) > 128:
        raise InvalidSample("Missing or invalid machine identifier")
    sample["machine_id"] = machine
    for name in ("speed", "average_speed"):
        sample[name] = number(payload.get(name), minimum=0)
    latitude = number(payload.get("latitude"), minimum=-90, maximum=90)
    longitude = number(payload.get("longitude"), minimum=-180, maximum=180)
    sample["latitude"], sample["longitude"] = (
        (latitude, longitude) if latitude is not None and longitude is not None else (None, None)
    )
    # memo is diagnostic free text, not needed for the pit dashboard API.
    return sample


def current_run(samples):
    """Discard earlier runs when the device clock, race timer, or lap resets.

    Input must be in AWS receipt order. No UTC time is inferred from device
    uptime, and no finish state is inferred from a stopped telemetry stream.
    """
    start = 0
    for index in range(1, len(samples)):
        previous, sample = samples[index - 1], samples[index]
        if (
            sample["machine_id"] != previous["machine_id"]
            or sample["timestamp_ms"] < previous["timestamp_ms"]
            or sample["total_time_ms"] < previous["total_time_ms"]
            or sample["lap_number"] < previous["lap_number"]
        ):
            start = index
    return samples[start:]


def finish_confirmed(sample):
    """The device includes its final lap only after finishing."""
    return bool(sample and sample["lap_number"] > 0
                and sample.get("completed_lap_times_ms") is not None
                and len(sample["completed_lap_times_ms"]) == sample["lap_number"])


def lap_summaries(samples):
    """Use only the newest packet; never reconstruct or retain earlier laps."""
    times = samples[-1].get("completed_lap_times_ms") if samples else None
    return [{"lap": index + 1, "time_ms": duration, "source": "device"}
            for index, duration in enumerate(times or [])]

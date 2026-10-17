"""Normalize delta-vega MQTT fields without exposing arbitrary stored attributes."""

from decimal import Decimal
import math


MAX_SAFE_INTEGER = 2**53 - 1


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
        sample[name] = number(payload.get(name), integer=True, minimum=1 if name == "lap_number" else 0, maximum=MAX_SAFE_INTEGER)
        if sample[name] is None:
            raise InvalidSample("Missing or invalid telemetry counters")
    if sample["lap_time_ms"] > sample["total_time_ms"]:
        raise InvalidSample("Lap time exceeds total time")
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


def lap_summaries(samples):
    """Infer completed laps from observed transitions; gaps stay unknown.

    The first sample of the next lap provides the previous lap's end on the
    race clock: total_time_ms - lap_time_ms. Duration is returned only if the
    start boundary is known. Corrections or skipped laps must not invent times.
    """
    results = []
    previous = None
    lap_start = 0 if samples and samples[0]["lap_number"] == 1 else None
    for sample in samples:
        if previous is not None and sample["lap_number"] != previous["lap_number"]:
            end = sample["total_time_ms"] - sample["lap_time_ms"]
            adjacent = sample["lap_number"] == previous["lap_number"] + 1
            duration = end - lap_start if adjacent and lap_start is not None else None
            results.append({
                "lap": previous["lap_number"],
                "time_ms": duration if duration is not None and duration > 0 else None,
                "source": "inferred_transition",
            })
            lap_start = end
        previous = sample
    return results

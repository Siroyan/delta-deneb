"""Loopback-only synthetic HTTP API for UI verification; never connects to AWS."""

import argparse
import base64
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

from backend.telemetry.model import finish_confirmed, lap_summaries


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "local-preview"
SCENARIOS = {"live", "stale", "empty", "gps-missing", "error", "unauthorized", "lap-2", "lap-3-partial", "latest-only", "waiting", "started", "finished", "zero-final", "lap-100", "legacy"}


class PreviewData:
    def __init__(self, scenario="live", *, details_delay=0):
        self.started = int(time.time() * 1000)
        self.scenario = scenario
        self.details_delay = details_delay
        self.course = json.loads((ROOT / "web/assets/course.json").read_text())

    def samples(self):
        if self.scenario == "empty":
            return []
        now = int(time.time() * 1000)
        if self.scenario == "stale":
            now = self.started - 30000
        if self.scenario in {"latest-only", "waiting", "started", "finished", "zero-final", "lap-100", "legacy"}:
            payload = json.loads((ROOT / "tests/fixtures/mqtt-completed-laps.json").read_text())
            payload.pop("memo", None)
            if self.scenario == "waiting":
                payload.update(lap_number=0, total_time_ms=0, lap_time_ms=0, completed_lap_times_ms=[], average_speed=None)
            elif self.scenario == "started":
                payload.update(lap_number=1, total_time_ms=1000, lap_time_ms=1000, completed_lap_times_ms=[])
            elif self.scenario == "finished":
                payload["completed_lap_times_ms"].append(45000)
            elif self.scenario == "zero-final":
                payload.update(lap_number=2, total_time_ms=362000, lap_time_ms=0, completed_lap_times_ms=[362000, 0])
            elif self.scenario == "lap-100":
                payload.update(lap_number=100, total_time_ms=100000, lap_time_ms=1000, completed_lap_times_ms=[1000] * 100)
            elif self.scenario == "legacy":
                payload.pop("completed_lap_times_ms")
            # Event-only waiting/finished packets retain their original receipt.
            receipt = self.started if self.scenario in {"waiting", "finished", "zero-final", "lap-100"} else now
            return [payload | {"sample_id": f"preview-{self.scenario}-{receipt}", "received_at_ms": receipt}]
        current_start = self.started - 22 * 60000
        elapsed = now - current_start
        if self.scenario in {"lap-2", "lap-3-partial"}:
            # Fixed counter snapshots for reproducing the lap-2 to lap-3 bug.
            elapsed = 301000 if self.scenario == "lap-2" else 631000
            current_start = now - elapsed
        result = []
        for name, start, elapsed, boot in [
            ("previous", self.started - 65 * 60000, 35 * 60000, 300000),
            ("current", current_start, elapsed, 120000),
        ]:
            elapsed = max(0, elapsed // 1000 * 1000)
            times = list(range(0, elapsed + 1, 5000))
            if times[-1] != elapsed:
                times.append(elapsed)
            for total in times:
                lap = min(7, total // 300000 + 1)
                lap_time = total - (lap - 1) * 300000
                if name == "current" and self.scenario == "lap-3-partial" and total >= 300000:
                    lap = 2 if total < 630000 else 3
                    lap_time = total - (300000 if lap == 2 else 630000)
                route = self.course["routes"]["first_lap" if lap == 1 else "final_lap" if lap == 7 else "regular_lap"]
                distance = min(route["length_m"], lap_time / 300000 * route["length_m"])
                points = route["points"]
                point = next((point for point in points if point["s"] >= distance), points[-1])
                result.append({
                    "sample_id": f"preview-{name}-{total}", "machine_id": "delta_machine_alpha",
                    "received_at_ms": start + total, "timestamp_ms": boot + total,
                    "total_time_ms": total, "lap_time_ms": lap_time, "lap_number": lap,
                    "completed_lap_times_ms": ([300000, 330000][:lap - 1] if name == "current" and self.scenario == "lap-3-partial" else [300000] * (lap - 1)),
                    "speed": round(27 + 8 * math.sin(total / 18000), 3), "average_speed": 26.5,
                    "latitude": None if self.scenario == "gps-missing" else point["latitude"],
                    "longitude": None if self.scenario == "gps-missing" else point["longitude"],
                })
        return sorted(result, key=lambda item: item["received_at_ms"])


def handler_class(data):
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(ROOT / "web"), **kwargs)

        def log_message(self, *_):
            pass

        def send_json(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            if status == 503:
                self.send_header("Retry-After", "3")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            if url.path == "/__preview/state":
                scenario = query.get("scenario", data.scenario)
                if scenario not in SCENARIOS:
                    return self.send_json(400, {"error": {"code": "invalid_request"}})
                if data.scenario != scenario:
                    data.started = int(time.time() * 1000)
                data.scenario = scenario
                return self.send_json(200, {"scenario": scenario})
            if not url.path.startswith("/api/"):
                return super().do_GET()
            if url.path == "/api/health":
                return self.send_json(200, {"status": "ok", "service": "delta-deneb", "stage": "local", "step": 2})
            if url.path not in {"/api/telemetry/latest", "/api/telemetry/history", "/api/telemetry/laps"}:
                return self.send_json(404, {"error": {"code": "not_found"}})
            if self.headers.get("X-Deneb-Token") != TOKEN or data.scenario == "unauthorized":
                return self.send_json(401, {"error": {"code": "unauthorized"}})
            if data.scenario == "error":
                return self.send_json(503, {"error": {"code": "temporarily_unavailable"}})
            if not url.path.endswith("/latest") and data.details_delay:
                time.sleep(data.details_delay)
            now = int(time.time() * 1000)
            if data.scenario == "latest-only" and not url.path.endswith("/latest"):
                return self.send_json(503, {"error": {"code": "temporarily_unavailable"}})
            samples = data.samples()
            if url.path.endswith("/latest"):
                latest = samples[-1] if samples else None
                return self.send_json(200, {"latest": latest, "received_at_ms": latest["received_at_ms"] if latest else None, "fetched_at_ms": now, "invalid_items": 0})
            try:
                if "cursor" in query:
                    page = json.loads(base64.urlsafe_b64decode(query["cursor"]))
                    start, end, offset = page["from"], page["to"], page["offset"]
                else:
                    end = int(query.get("to_ms", now))
                    start = int(query.get("from_ms", max(0, end - 7200000)))
                    offset = 0
                limit = int(query.get("limit", 300))
                if not 0 <= start <= end <= now or end - start > 7200000 or not 1 <= limit <= 1000:
                    raise ValueError
            except (ValueError, TypeError, KeyError):
                return self.send_json(400, {"error": {"code": "invalid_request"}})
            samples = [item for item in samples if start <= item["received_at_ms"] <= end]
            common = {"from_ms": start, "to_ms": end, "invalid_items": 0, "fetched_at_ms": now}
            if url.path.endswith("/history"):
                if data.scenario == "lap-3-partial":
                    # The index has only the newest item available in this view.
                    return self.send_json(200, common | {"history": samples[-1:], "next_cursor": None})
                more = offset + limit < len(samples)
                cursor = base64.urlsafe_b64encode(json.dumps({"from": start, "to": end, "offset": offset + limit}).encode()).decode() if more else None
                return self.send_json(200, common | {"history": samples[offset:offset + limit], "next_cursor": cursor})
            latest = samples[-1] if samples else None
            return self.send_json(200, common | {
                "laps": lap_summaries([latest] if latest else []), "latest": latest,
                "truncated": False, "finish_confirmed": finish_confirmed(latest),
                "lap_times_available": latest is not None and latest.get("completed_lap_times_ms") is not None,
            })
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), default="live")
    parser.add_argument("--details-delay", type=float, default=0, help="Delay history/lap responses by 0–30 seconds; latest remains immediate")
    args = parser.parse_args()
    if not 0 <= args.details_delay <= 30:
        parser.error("--details-delay must be between 0 and 30 seconds")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_class(PreviewData(args.scenario, details_delay=args.details_delay)))
    print(f"Synthetic preview (no AWS): http://127.0.0.1:{args.port}/#token={TOKEN}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

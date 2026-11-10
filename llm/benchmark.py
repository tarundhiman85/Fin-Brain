"""Content-free classification or live API latency benchmarks."""
import argparse
import asyncio
import json
import statistics
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from config import get_settings
from llm.policy import RoutingPolicyError, classify

CASES = [
    ("routine_quote", "What is the AAPL stock quote?"),
    ("public_risk", "Compare AAPL and MSFT public market risk."),
    ("private_audit", "Review my private bank statement."),
]


def summary(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"p50_ms": round(statistics.median(values), 3),
            "p95_ms": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3)}


async def benchmark(args):
    settings = get_settings()
    rows = []
    if args.live:
        url = urlparse(args.base_url)
        if (url.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
                or url.scheme not in {"http", "https"} or url.username or url.password
                or url.query or url.fragment):
            raise ValueError("Benchmark API URL must be local.")
    async with httpx.AsyncClient(timeout=900, trust_env=False, follow_redirects=False) as client:
        for label, message in CASES:
            for mode in args.modes:
                for _ in range(args.iterations):
                    started = perf_counter()
                    row = {"case": label, "mode": mode}
                    if args.live:
                        request_id = str(uuid4())
                        row["request_id"] = request_id
                        try:
                            response = await client.post(
                                args.base_url.rstrip("/") + "/api/v1/agent/chat",
                                headers={"X-API-Key": settings.api_key.get_secret_value()},
                                json={"message": message, "routing_mode": mode,
                                      "conversation_id": "benchmark-" + uuid4().hex,
                                      "request_id": request_id},
                            )
                            row["http_status"] = response.status_code
                            if response.status_code == 200:
                                data = response.json()
                                events = data.get("routing_events", [])
                                row.update(
                                    status=data["status"],
                                    model_ms=round(sum(event["duration_ms"] for event in events), 3),
                                    cloud_calls=sum(event["engine"] == "cloud" for event in events),
                                    local_calls=sum(event["engine"] == "local" for event in events),
                                )
                            else:
                                row["status"] = "blocked" if response.status_code == 403 else "http_error"
                        except httpx.HTTPError:
                            row["status"] = "transport_error"
                    else:
                        try:
                            route = classify(message, settings, mode)
                            row.update(status="classified", engine=route.engine, reason=route.reason)
                        except RoutingPolicyError:
                            row["status"] = "blocked"
                    row["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
                    rows.append(row)
    groups = []
    for label, _ in CASES:
        for mode in args.modes:
            selected = [row for row in rows if row["case"] == label and row["mode"] == mode]
            successful = [row["elapsed_ms"] for row in selected
                          if row["status"] in {"completed", "classified"}]
            groups.append({"case": label, "mode": mode, "successful_samples": len(successful),
                           "latency": summary(successful),
                           "cloud_calls": sum(row.get("cloud_calls", 0) for row in selected)})
    return {"benchmark": "live_agent" if args.live else "classifier_only",
            "summary": groups, "samples": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Contact the running API and configured models")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=["auto", "local_only", "cloud_only"],
                        default=["auto", "local_only"])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    if not 1 <= args.iterations <= 10000:
        parser.error("--iterations must be between 1 and 10000")
    print(json.dumps(asyncio.run(benchmark(args)), indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/python3

"""capture_samples.py — Subscribe to aisstream.io briefly and dump raw envelopes.

Used during initial schema design so the ECS field-mapping can be locked
against the exact JSON shapes the upstream emits, rather than guessing from
the documentation. NOT part of the production ingester.

Usage:
    export AISSTREAM_API_KEY=...
    python3 capture_samples.py --bbox 33.90,-81.00,32.00,-77.50 \\
        --duration 60 --output sample.json
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

import websockets

AIS_STREAM_URL = "wss://stream.aisstream.io/v0/stream"


def parse_bbox(arg: str):
    parts = [float(x) for x in arg.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "bbox must be NW_LAT,NW_LON,SE_LAT,SE_LON")
    nw_lat, nw_lon, se_lat, se_lon = parts
    return [[nw_lat, nw_lon], [se_lat, se_lon]]


async def capture(api_key, bounding_boxes, duration_s, output_path,
                  per_type_cap):
    subscribe = {
        "APIKey": api_key,
        "BoundingBoxes": [bounding_boxes],
    }
    type_counts = Counter()
    kept_per_type = Counter()
    records = []
    deadline = time.monotonic() + duration_s

    print(f"connecting to {AIS_STREAM_URL} ...", flush=True)
    async with websockets.connect(AIS_STREAM_URL) as ws:
        await ws.send(json.dumps(subscribe))
        print(f"subscribed. capturing for {duration_s}s "
              f"(cap {per_type_cap} per MessageType) ...", flush=True)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("MessageType", "unknown")
            type_counts[mtype] += 1
            if kept_per_type[mtype] < per_type_cap:
                records.append(msg)
                kept_per_type[mtype] += 1

    Path(output_path).write_text(
        json.dumps({"records": records, "type_counts": dict(type_counts)},
                   indent=2))
    print(f"\ncaptured {sum(type_counts.values())} messages, "
          f"kept {len(records)} samples.\nMessageType breakdown:")
    for t, n in sorted(type_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {t:40s} total={n:6d}  kept={kept_per_type[t]}")
    print(f"\nwrote {output_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", type=parse_bbox, required=True,
                    help="NW_LAT,NW_LON,SE_LAT,SE_LON (one box)")
    ap.add_argument("--duration", type=int, default=60,
                    help="capture duration seconds (default 60)")
    ap.add_argument("--per-type-cap", type=int, default=5,
                    help="max samples retained per MessageType (default 5)")
    ap.add_argument("--output", default="sample.json")
    ap.add_argument("--api-key-env", default="AISSTREAM_API_KEY")
    args = ap.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(f"error: {args.api_key_env} not set in environment",
              file=sys.stderr)
        sys.exit(1)

    asyncio.run(capture(api_key, args.bbox, args.duration, args.output,
                        args.per_type_cap))


if __name__ == "__main__":
    main()

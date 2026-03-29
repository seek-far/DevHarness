#!/usr/bin/env python3
"""
Send a pipeline webhook message to the gateway.

Reads a JSON payload from pipeline_msg.txt (same directory) and POSTs it
to the gateway's /webhook endpoint.

Usage:
  python test_utility/send_pipeline_msg.py
  python test_utility/send_pipeline_msg.py --gateway-url http://localhost:8000
  python test_utility/send_pipeline_msg.py --file /path/to/custom_msg.txt
"""

import argparse
import json
import sys
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description="Send pipeline message to gateway")
    parser.add_argument(
        "--gateway-url",
        default="http://localhost:8000",
        help="Gateway base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--file",
        default=str(Path(__file__).parent / "pipeline_msg.txt"),
        help="Path to JSON payload file (default: pipeline_msg.txt in same directory)",
    )
    args = parser.parse_args()

    msg_path = Path(args.file)
    if not msg_path.exists():
        print(f"Error: file not found: {msg_path}", file=sys.stderr)
        sys.exit(1)

    with open(msg_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    url = f"{args.gateway_url.rstrip('/')}/webhook"
    print(f"POST {url}")
    print(f"Payload source: {msg_path}")

    resp = httpx.post(url, json=payload)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.json()}")

    if resp.status_code != 200:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
tools/runrecord_exporter.py — long-running HTTP exporter for the RunRecord
journal. K8s-shaped sibling of tools/runrecord_to_metrics.py.

The runrecord-to-metrics script was built for the bare-host pattern: cron
periodically writes a .prom file that node_exporter's textfile collector
picks up. On Kubernetes the textfile-collector path is awkward (CronJob +
shared PVC + node_exporter sidecar), so we expose the same metrics over
HTTP instead.

This exporter is a thin wrapper: every GET /metrics triggers a fresh
_scan_journal + _aggregate + render against the journal directory at
$BF_JOURNAL_DIR (default /var/sdlcma/journal). Freshness is controlled by
the Prometheus scrape interval, not by a cron cadence. The journal dir is
expected to be mounted RO from a hostPath (or RWX PVC on multi-node).

Stdlib only — no FastAPI or prometheus_client dependency. Code reuse with
the textfile path is total: same _scan_journal, same _aggregate, same
render. Switching deployment shapes does not change the metric names,
labels, or values.

Run:
    BF_JOURNAL_DIR=/var/sdlcma/journal \\
    EXPORTER_PORT=9103 \\
    python -m tools.runrecord_exporter
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Sibling import — same module, same functions, same output shape.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.runrecord_to_metrics import _aggregate, _scan_journal, render  # noqa: E402

logger = logging.getLogger("runrecord_exporter")


def _build_handler(journal_dir: Path):
    """Closure over journal_dir → BaseHTTPRequestHandler subclass.

    Closure pattern (vs class attr) makes the handler testable: one
    `_build_handler(tmp_path)` per test, no shared global state.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path != "/metrics":
                self.send_response(404)
                self.end_headers()
                return
            try:
                body = render(
                    _aggregate(_scan_journal(journal_dir)),
                    scan_ts=time.time(),
                )
            except Exception:
                # A pathological journal entry must NOT poison the scrape.
                # 500 keeps Prometheus's `up{}` metric on this target at 0
                # so the operator sees the outage in Grafana.
                logger.exception("scrape failed")
                self.send_response(500)
                self.end_headers()
                return
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        # Quiet the default per-request access log — Prometheus polls
        # every 15s and the stdout noise drowns out actual errors.
        def log_message(self, format, *args):  # noqa: A002
            return

    return Handler


def serve(journal_dir: Path, host: str, port: int) -> ThreadingHTTPServer:
    """Build + return a started HTTPServer. Caller owns lifecycle.

    Returns started server for the in-process tests; main() runs
    serve_forever() in the calling thread for the prod entry point.
    """
    server = ThreadingHTTPServer((host, port), _build_handler(journal_dir))
    logger.info("runrecord exporter listening on http://%s:%d/metrics "
                "(journal=%s)", host, port, journal_dir)
    return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--journal-dir",
        default=os.environ.get("BF_JOURNAL_DIR", "/var/sdlcma/journal"),
        help="Directory holding <ts>_<bug>_<agent>/record.json subdirs",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("EXPORTER_HOST", "0.0.0.0"),
        help="Bind address (default 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("EXPORTER_PORT", "9103")),
        help="Listen port (default 9103)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [exporter] %(message)s",
        stream=sys.stdout,
    )
    server = serve(Path(args.journal_dir), args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

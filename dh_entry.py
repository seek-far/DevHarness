import subprocess
import sys
import signal
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s [dh_entry %(name)s:%(funcName)s:%(lineno)d] %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

procs = []

def shutdown(*_):
    for p in procs:
        if p.poll() is None:
            p.terminate()

def main():
    global procs

    gateway = subprocess.Popen([
        sys.executable, "-m", "uvicorn",
        "gateway.gateway:app",
        "--host", "127.0.0.1",
        "--port", "8000",
    ])

    orchestrator = subprocess.Popen([
        sys.executable, "-m", "orchestrator.orchestrator",
    ])

    procs = [gateway, orchestrator]

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        exit_codes = [p.wait() for p in procs]
        raise SystemExit(max(exit_codes))
    finally:
        shutdown()

if __name__ == "__main__":
    main()
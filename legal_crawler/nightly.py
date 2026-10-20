"""Nightly runner: crawl -> build v2 drafts (+ relations) -> publish. Publish only writes when ES_WRITE_ENABLED=yes;

    python -m legal_crawler.nightly --once      # run now (this is what cron calls)
    python -m legal_crawler.nightly             # sleep until NIGHTLY_AT (default 00:00 local time), then run, forever
Otherwise the publish step is a dry run. LLM concurrency: 3 from 07:00 to 22:00, 10 from 22:00 to 07:00.
"""
import argparse
import fcntl
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .settings import load_env

log = logging.getLogger("nightly")


def seconds_until(at):
    hour, minute = (int(x) for x in at.split(":"))
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return ((target if target > now else target + timedelta(days=1)) - now).total_seconds()


def run_once(data):
    data.mkdir(parents=True, exist_ok=True)
    with (data / "nightly.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning("Previous run still active; skipping this one")
            return 0
        started = time.time()
        env = {**os.environ, "DATA_DIR": str(data)}
        write = os.getenv("ES_WRITE_ENABLED", "no").lower() == "yes"
        steps = [[sys.executable, "-m", "legal_crawler", "crawl"],
                 [sys.executable, "-m", "legal_crawler.v2.export", "--data", str(data), "--new-only"]]
        code = 0
        for step in steps:
            log.info("Running: %s", " ".join(step[1:]))
            result = subprocess.run(step, env=env)
            if result.returncode not in (0, 2):  # 2 = crawl finished with some failed jobs
                log.error("Step failed with exit code %d", result.returncode)
                return result.returncode
        drafts = sorted(d for d in (data / "v2_draft").glob("2*") if d.is_dir() and d.stat().st_mtime >= started)
        if not drafts or not (drafts[-1] / "documents.jsonl").exists():
            log.info("No new drafts this run; nothing to publish")
            return code
        step = [sys.executable, "-m", "legal_crawler.v2.publish", "--draft", str(drafts[-1]), "--data", str(data)] + (["--write"] if write else [])
        log.info("Publishing %s (%s)", drafts[-1].name, "WRITE" if write else "dry run: ES_WRITE_ENABLED is not 'yes'")
        result = subprocess.run(step, env=env)
        if result.returncode != 0:
            log.error("Publish finished with failures (exit %d); see quarantine and publish log", result.returncode)
            code = result.returncode
        return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--data", type=Path, default=Path(os.getenv("DATA_DIR", "data")))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env(Path(".env"))
    if args.once:
        return run_once(args.data)
    at = os.getenv("NIGHTLY_AT", "00:00")
    while True:
        wait = seconds_until(at)
        log.info("Next run at %s (in %.0f s)", at, wait)
        time.sleep(wait)
        run_once(args.data)


if __name__ == "__main__":
    sys.exit(main())

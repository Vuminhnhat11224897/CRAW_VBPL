import argparse
import fcntl
import json
import logging
from pathlib import Path

from .crawler import Crawler
from .http import Client, FetchError
from .settings import Settings
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Collect legal documents through TMProxy; settings in .env")
    parser.add_argument("command", choices=["crawl", "status", "export", "check-config", "migrate-files"])
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings = Settings.read(args.env_file)
        if args.command == "check-config":
            print(json.dumps({"sources": list(settings.configs), "date_from": settings.date_from,
                "date_to": settings.date_to, "proxy_key_configured": bool(settings.api_key),
                "data_dir": str(settings.data), "max_pages_per_source": settings.max_pages}, ensure_ascii=False, indent=2))
            return 0
        settings.data.mkdir(parents=True, exist_ok=True)
        with (settings.data / "crawler.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if args.command != "migrate-files":
                    raise ValueError("Another crawler is using DATA_DIR") from None
                logging.info("Waiting for the current crawler to stop before moving files")
                fcntl.flock(lock, fcntl.LOCK_EX)
            store = Store(settings.data)
            client = None
            try:
                if args.command in {"crawl", "export", "migrate-files"}:
                    migrated = store.migrate_files()
                    if migrated:
                        logging.info("Moved %d file records into source folders", migrated)
                if args.command == "migrate-files":
                    print(store.export())
                    return 0
                if args.command == "status":
                    print(json.dumps(store.stats(), indent=2))
                    return 0
                if args.command == "export":
                    print(store.export())
                    return 0
                client = Client(settings)
                # Fail once before queueing jobs if the key/network is unusable.
                client.proxy.get()
                failures = Crawler(settings, store, client).run()
                print(json.dumps(store.stats(), indent=2))
                return 2 if failures else 0
            finally:
                if args.command == "crawl":
                    store.export()
                if client:
                    client.close()
                store.db.close()
    except (ValueError, OSError, FetchError) as exc:
        logging.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        logging.warning("Stopped. Run the same command to resume.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

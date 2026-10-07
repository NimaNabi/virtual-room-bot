"""Entry point: python -m vrbot"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time

from .config import Settings
from .logging_setup import setup_logging

log = logging.getLogger("vrbot")


def _idle_without_token(settings: Settings) -> None:
    """No token yet: stay up (no crash loop), report clearly, and mark the container unhealthy."""
    log.error("DISCORD_TOKEN is not set. Windows: run Setup.cmd (or menu → Change bot token). "
                            "Docker: run scripts/set-token.sh, then `docker compose up -d`.")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    while True:
        (settings.data_dir / "heartbeat.json").write_text(json.dumps({"ts": time.time(), "state": "no_token"}))
        time.sleep(60)


def main() -> None:
    settings = Settings.from_env()
    setup_logging(settings.log_level)
    if not settings.token:
        _idle_without_token(settings)
        return
    from .bot import ServerBot

    bot = ServerBot(settings)
    try:
        asyncio.run(bot.start(settings.token))
    except KeyboardInterrupt:
        pass
    except Exception as e:  # noqa: BLE001
        log.critical("fatal: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

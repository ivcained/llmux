"""Run the LLMUX routing proxy: ``python -m llmux_core``."""

from __future__ import annotations

import argparse
import logging
from dataclasses import replace

from .app import build_proxy
from .config import load_settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the LLMUX routing proxy")
    parser.add_argument("--host", help="bind host (local only unless LLMUX_ALLOW_REMOTE=1)")
    parser.add_argument("--port", type=int, help="bind port")
    parser.add_argument("--policy", choices=["cheapest", "fastest", "balanced"])
    parser.add_argument("--tps-floor", choices=["low", "mid", "high"])
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    if args.host or args.port or args.policy or args.tps_floor:
        settings = replace(
            settings,
            host=args.host or settings.host,
            port=args.port or settings.port,
            policy=args.policy or settings.policy,
            tps_floor=args.tps_floor or settings.tps_floor,
        )
    proxy = build_proxy(settings)
    port = proxy.start(settings.host, settings.port)
    # Keep the market cache live so the card never waits on the marketplaces.
    proxy.start_warming()
    logging.getLogger("llmux").info(
        "LLMUX proxy listening on http://%s:%s/v1 (policy=%s, floor=%s)",
        settings.host,
        port,
        settings.policy,
        settings.tps_floor or "off",
    )
    try:
        proxy.wait()  # serve until interrupted
    except KeyboardInterrupt:
        pass
    finally:
        proxy.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Run opal-talk-svc: `opal-talk-svc [--config PATH] [--host H] [--port P]`."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from opal_talk.config import load_config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="opal-talk-svc", description="OPAL Talk TTS REST service")
    parser.add_argument("--config", help="service YAML config (default: $OPAL_TALK_CONFIG or ./config/service.yaml)")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port
    logging.basicConfig(level=config.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from opal_talk.api.app import create_app

    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=config.log_level.lower())


if __name__ == "__main__":
    main()

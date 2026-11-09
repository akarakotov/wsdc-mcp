"""Run the server: `python -m wsdc_mcp` (configuration via WSDC_MCP_* environment variables)."""

from __future__ import annotations

import logging
from collections.abc import Callable

import uvicorn
from starlette.types import ASGIApp

from . import usage
from .app import create_app
from .config import Config, load_config


def serve(build: Callable[[Config], ASGIApp] = create_app) -> None:
    """Configure logging and run `build(config)` with uvicorn behind the local reverse proxy."""
    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if config.usage_log:
        usage.log_to_file(config.usage_log)
    uvicorn.run(
        build(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
    )


def main() -> None:
    serve()


if __name__ == "__main__":
    main()

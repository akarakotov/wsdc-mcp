"""Run the server: `python -m wsdc_mcp` (configuration via WSDC_MCP_* environment variables)."""

from __future__ import annotations

import logging

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    config = load_config()
    logging.basicConfig(level=config.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(
        create_app(config),
        host=config.host,
        port=config.port,
        log_level=config.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        access_log=False,
    )


if __name__ == "__main__":
    main()

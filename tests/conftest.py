from __future__ import annotations

import os
from pathlib import Path

import pytest

from wsdc_mcp.config import Config
from wsdc_mcp.db import CACHE

ROOT = Path(__file__).resolve().parents[1]
# The rulebook text is not part of the public repo; tests that need it skip without it.
RULES_FILE = Path(os.environ.get("WSDC_MCP_RULES_PATH", ROOT.parent / "docs/rules_text/WSDCPointsRegistryDocument_2026.1B.txt"))
TEST_DSN = os.environ.get("WSDC_MCP_TEST_DSN")


def make_config(**overrides) -> Config:
    base = dict(
        db_dsn=None,
        public_url="https://wsdcpoints.test",
        host="127.0.0.1",
        port=8001,
        rules_path=None,
        rules_version=None,
        pool_max_size=2,
        statement_timeout_ms=5000,
        registry_auth=None,
        contact_email=None,
        repo_url="https://github.com/akarakotov/wsdc-mcp",
        log_level="INFO",
    )
    base.update(overrides)
    return Config(**base)


@pytest.fixture(autouse=True)
def _clear_cache():
    CACHE.clear()
    yield
    CACHE.clear()


@pytest.fixture
def config() -> Config:
    return make_config()

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nexus.config import GraphStoreCfg


def test_graph_store_config_rejects_empty_host() -> None:
    with pytest.raises(ValidationError):
        GraphStoreCfg(host="")


def test_graph_store_config_rejects_bad_port() -> None:
    with pytest.raises(ValidationError):
        GraphStoreCfg(port=0)

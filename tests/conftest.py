from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clear_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NEXUS_ADMIN_API_KEY",
        "NEXUS_BOOTSTRAP_ADMIN_EMAIL",
        "NEXUS_BOOTSTRAP_ADMIN_PASSWORD",
        "NEXUS_ENV",
        "NEXUS_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

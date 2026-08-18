"""Test fixtures.

Nothing here uses a real account id, household id or list uuid — this repo is
public, and the soak harness that produced the real ones is not.
"""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture
def custom_integration(enable_custom_integrations):
    """Let Home Assistant load this integration.

    Deliberately not autouse. It drags a whole Home Assistant into every test
    that requests it, and api.py has no Home Assistant in it — making this
    automatic started HA for tests that never needed it, and left one of its
    threads running past the cleanup check.
    """
    yield

"""Test fixtures.

Nothing here uses a real account id, household id or list uuid — this repo is
public, and the soak harness that produced the real ones is not.
"""

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant see custom_components/ during tests."""
    yield

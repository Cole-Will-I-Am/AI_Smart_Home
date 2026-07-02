import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from homeops import build_world


@pytest.fixture
def world():
    """Full world with local-first automations registered."""
    return build_world()


@pytest.fixture
def bare():
    """World WITHOUT automations — isolates permission-engine / router behaviour."""
    return build_world(register_automations=False)

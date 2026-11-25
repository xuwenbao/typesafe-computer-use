"""Agents are built by name, and the model SDK stays unloaded until a takeover actually starts."""

import sys

import pytest

from yuanji_computer_use.agents.base import AgentFactory


def test_an_unknown_name_is_refused():
    with pytest.raises(KeyError):
        AgentFactory.create("nobody")


def test_building_the_agents_does_not_load_the_model_sdk():
    """Creating a role only imports its module. The SDK loads when a takeover calls it, not here.

    Another test in this process may already have imported the SDK, so the check is that these
    four constructions do not change whether it is loaded.
    """
    loaded = "agents" in sys.modules
    for name in ("host", "policy", "writer", "driver"):
        agent = AgentFactory.create(name)
        assert agent.name == name
    assert ("agents" in sys.modules) is loaded

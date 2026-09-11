"""Render the actual blueprint condition against representative HA transitions."""

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from jinja2 import Environment, StrictUndefined


class BlueprintLoader(yaml.SafeLoader):
    pass


BlueprintLoader.add_constructor("!input", lambda loader, node: loader.construct_scalar(node))
BLUEPRINT = yaml.load(
    Path("homeassistant/integration/blueprints/beluga_print_alerts.yaml").read_text(),
    Loader=BlueprintLoader,
)
CONDITION = Environment(undefined=StrictUndefined).from_string(
    BLUEPRINT["conditions"][0]["value_template"]
)


@pytest.mark.parametrize(("kind", "old", "new", "expected"), [
    ("phase", "printing", "finished", True),
    ("phase", "paused", "completed", True),
    ("phase", "preparing", "failed", True),
    ("phase", "printing", "paused", True),
    ("phase", "paused", "paused", False),
    ("phase", "finished", "finished", False),
    ("phase", "unknown", "finished", False),
    ("phase", "unavailable", "paused", False),
    ("phase", "printing", "unavailable", False),
    ("phase", "idle", "finished", False),
    ("problem", "off", "on", True),
    ("problem", "on", "on", False),
    ("problem", "unavailable", "on", False),
])
def test_observed_transition(kind, old, new, expected):
    trigger = SimpleNamespace(
        id=kind,
        from_state=SimpleNamespace(state=old, attributes={}),
        to_state=SimpleNamespace(state=new, attributes={}),
    )
    assert (CONDITION.render(trigger=trigger).strip() == "True") == expected


def test_new_problem_code_alerts_without_repeated_notifications_for_same_code():
    old = SimpleNamespace(state="on", attributes={"code": "a"})
    new = SimpleNamespace(state="on", attributes={"code": "b"})
    trigger = SimpleNamespace(id="problem", from_state=old, to_state=new)
    assert CONDITION.render(trigger=trigger).strip() == "True"
    trigger.from_state = new
    assert CONDITION.render(trigger=trigger).strip() == "False"


def test_entity_creation_does_not_replay_a_historical_completion():
    trigger = SimpleNamespace(
        id="phase", from_state=None,
        to_state=SimpleNamespace(state="finished", attributes={}),
    )
    assert CONDITION.render(trigger=trigger).strip() == "False"

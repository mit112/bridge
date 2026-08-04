"""Module-macro tests for `_launch.html`.

Task 5 parameterized `handoff_block`/`launch_band` (and `handoff_actions`) to
take an explicit `handoff` argument instead of reading `card.handoff` (the
newest-of compat property) -- the only way `_workspace_current.html` can loop
`for h in card.handoffs` and render one fireable block per queued handoff
rather than just the newest.

These tests render the macros directly off the compiled Jinja module (the
same harness pattern as `tests/test_components.py`), calling each macro twice
with two different handoffs on the SAME card. That is the only way to prove
the macro derives every id/field from its `handoff` parameter -- a card-level
regression back to `card.handoff` would make both calls emit identical output.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from bridge.api import _ago, _ago_epoch, _kilo
from bridge.cards import spark_points
from bridge.config import ModelChoice, PermissionChoice
from bridge.models import Card

TPL = Path(__file__).resolve().parent.parent / "src" / "bridge" / "templates"


def _module():
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    env.filters["ago"] = _ago
    env.filters["ago_epoch"] = _ago_epoch
    env.filters["kilo"] = _kilo
    env.filters["spark_points"] = spark_points
    return env.get_template("_launch.html").module


def _card(**overrides):
    fields = dict(
        project_id=7,
        path="/x/demo",
        name="Demo",
        session=None,
        git=None,
        tokens_today=0,
        tokens_5h=0,
        launch_models=[ModelChoice("sonnet", "Sonnet 5")],
        launch_efforts=["medium"],
        launch_permission_modes=[PermissionChoice("", "Ask as usual")],
    )
    fields.update(overrides)
    return Card(**fields)


def _handoff(hid, **overrides):
    fields = dict(
        id=hid,
        next_prompt=f"prompt for {hid}",
        summary=f"Summary {hid}",
        created_at=1,
        suggested_model=None,
        suggested_effort=None,
    )
    fields.update(overrides)
    return fields


def test_handoff_block_renders_from_the_passed_handoff_not_a_card_singleton():
    card = _card()
    mod = _module()

    html1 = mod.handoff_block(card, _handoff("h1"), None)
    html2 = mod.handoff_block(card, _handoff("h2"), None)

    assert 'data-handoff-section="h1"' in html1
    assert 'data-handoff-section="h2"' not in html1
    # `data-launch-handoff` is `launch_band`'s hook, not `handoff_block`'s.
    assert "data-launch-handoff" not in html1

    assert 'data-handoff-section="h2"' in html2
    assert 'data-handoff-section="h1"' not in html2


def test_handoff_block_renders_nothing_for_a_falsy_handoff():
    html = _module().handoff_block(_card(), None, None)
    assert html.strip() == ""


def test_launch_band_keys_data_launch_handoff_off_the_passed_handoff():
    card = _card()
    mod = _module()

    band1 = mod.launch_band(card, _handoff("h1"))
    band2 = mod.launch_band(card, _handoff("h2"))

    assert 'data-launch-handoff="h1"' in band1
    assert 'data-launch-handoff="h2"' not in band1
    assert 'data-launch-handoff="h2"' in band2
    assert 'data-launch-handoff="h1"' not in band2


def test_launch_band_with_no_handoff_renders_disabled_start_session():
    band = _module().launch_band(_card(), None, primary=True)
    assert "Start session" in band
    assert "data-launch-handoff" not in band
    button = band.split('data-launch-button="', 1)[1]
    assert "disabled" in button.split(">", 1)[0]

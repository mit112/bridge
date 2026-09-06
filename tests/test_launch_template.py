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

from bridge.api import register_template_filters
from bridge.config import ModelChoice, PermissionChoice
from bridge.models import Card, SessionRecord

TPL = Path(__file__).resolve().parent.parent / "src" / "bridge" / "templates"


def _module():
    env = Environment(loader=FileSystemLoader(str(TPL)), autoescape=True)
    register_template_filters(env)
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


def test_handoff_block_title_is_its_own_sessions_not_the_cards_singleton():
    """The h2 title comes off `handoff.session_title`, not `card.session.title`.

    `card.session` is the project's overall latest session, which is a
    DIFFERENT session than either handoff's own source here -- pinning the
    fix for a bug where every queued handoff on a project showed whichever
    session most recently started, regardless of which session wrote it.
    """
    card = _card(session=SessionRecord(
        session_id="latest", transcript_path="/t/latest", title="unrelated later work",
    ))
    mod = _module()

    html1 = mod.handoff_block(card, _handoff("h1", session_title="earlier work"), None)
    html2 = mod.handoff_block(card, _handoff("h2", session_title="later work"), None)

    assert "earlier work" in html1
    assert "unrelated later work" not in html1
    assert "later work" not in html1  # substring of "earlier work" would be a false pass

    assert "later work" in html2
    assert "unrelated later work" not in html2


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


def test_handoff_block_keys_the_section_on_the_handoff_id_for_the_morph():
    """morph.js matches children by `id`/`data-key` and falls back to POSITION.

    Without a key the live morph reconciled a replaced handoff into the old
    section, and `data-live-preserve` on the prompt `<details>` stopped the
    walk before the textarea -- so the visible title changed while the textarea
    kept the previous handoff's id and text, and Save PATCHed that stale row.
    """
    card = _card()
    mod = _module()

    assert 'data-key="h1"' in mod.handoff_block(card, _handoff("h1"), None)
    assert 'data-key="h2"' in mod.handoff_block(card, _handoff("h2"), None)


# --- Lane D: a queued handoff a later session has overtaken -----------------
#
# `session_since` comes off `store.queued_handoffs` and says a session in this
# project STARTED after the prompt was written. Supersession is scoped to the
# source session, so nothing retires these rows -- the panel showed six equally
# loud "Ready" badges with no way to tell which prompt was still real.


def test_a_demoted_handoff_states_the_fact_instead_of_claiming_ready():
    mod = _module()
    card = _card()

    fresh = mod.handoff_block(card, _handoff("h1", session_since=False), None)
    demoted = mod.handoff_block(card, _handoff("h2", session_since=True), None)

    assert ">Ready</span>" in fresh
    assert "A session has run since" not in fresh
    assert ">A session has run since</span>" in demoted
    assert ">Ready</span>" not in demoted
    # Colour is never the only cue, and the quiet pill is an existing one.
    assert "pill--idle" in demoted
    assert "pill--work" not in demoted


def test_the_demotion_is_part_of_the_sections_accessible_name():
    """A sighted user reads the badge; a screen-reader user must get the same
    words. The pill is listed in `aria-labelledby` alongside the title, so the
    section announces "<title>, A session has run since" -- not a bare title
    whose demotion exists only as a CSS class."""
    demoted = _module().handoff_block(_card(), _handoff("h2", session_since=True), None)

    assert 'aria-labelledby="handoff-h2-title handoff-h2-state"' in demoted
    assert 'id="handoff-h2-state">A session has run since</span>' in demoted


def test_a_demoted_handoff_hides_its_prompt_behind_one_disclosure():
    """Collapsed by default: the row is the kicker, the title and the badge.

    The summary and the full prompt field both move inside a single
    `<details>` -- the freshest handoff keeps the open `handoff-prompt` box,
    so the two do not render at the same weight. The textarea itself still
    exists in the DOM (a closed `<details>` does not remove it), which is what
    keeps the launch band's `data-launch-prompt` lookup working unopened.
    """
    mod = _module()
    card = _card()

    fresh = mod.handoff_block(
        card, _handoff("h1", session_since=False), None, collapse_prompt=True)
    demoted = mod.handoff_block(
        card, _handoff("h2", session_since=True), None, collapse_prompt=True)

    assert "handoff__demoted-body" not in fresh
    assert 'class="handoff-prompt"' in fresh

    assert "handoff__demoted-body" in demoted
    # The open preview box is gone -- there is one disclosure, not two.
    assert 'class="handoff-prompt"' not in demoted
    assert 'class="handoff__preview"' not in demoted
    # The prompt field is still present, inside the collapsed disclosure.
    body = demoted.split("handoff__demoted-body", 1)[1].split("</details>", 1)[0]
    assert 'data-prompt-handoff="h2"' in body
    assert "Summary h2" in body


def test_the_title_prefers_the_summarys_first_line_to_the_boilerplate():
    """With no session title the card used to read "Demo is ready to continue",
    which names the project and says nothing about the work. The summary's
    first line is the nearest thing to a title the handoff actually carries;
    the boilerplate survives only when there is no summary either."""
    mod = _module()
    card = _card()

    with_summary = mod.handoff_block(
        card,
        _handoff("h1", session_title=None, summary="Land the parser fix\nthen retest"),
        None)
    assert ">Land the parser fix</h2>" in with_summary
    assert "is ready to continue" not in with_summary
    # Only the FIRST line reaches the heading; the rest stays in the summary.
    assert "then retest" not in with_summary.split("</h2>", 1)[0]
    assert "then retest" in with_summary

    no_summary = mod.handoff_block(
        card, _handoff("h2", session_title=None, summary=None), None)
    assert ">Demo is ready to continue</h2>" in no_summary

    # A real session title still wins over both.
    titled = mod.handoff_block(
        card, _handoff("h3", session_title="Ship the release", summary="Land the parser fix"),
        None)
    assert ">Ship the release</h2>" in titled
    assert "Land the parser fix</h2>" not in titled


def test_every_launch_band_offers_the_same_labelled_button():
    """Rows 2..n of a stacked handoff list used to get a bare `▶` glyph while
    row 1 got "Continue in Terminal" -- two different-looking controls for one
    action. Every band now renders the same labelled button; `primary` only
    decides which one wears `btn--primary`."""
    mod = _module()
    card = _card()

    first = mod.launch_band(card, _handoff("h1"), primary=True)
    rest = mod.launch_band(card, _handoff("h2"), primary=False)

    for band in (first, rest):
        assert "Continue in Terminal" in band
        assert "▶" not in band
        assert "btn--icon" not in band
        assert 'aria-label="Continue a session for Demo in Terminal"' in band

    assert "btn--primary" in first
    assert "btn--primary" not in rest

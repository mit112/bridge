"""The request bodies every write route validates against, and the two status
vocabularies the routes and templates share.

Kept out of `api` so a router module can import the model it validates without
importing the route module that mounts it -- with the models inline, every
`routes_*` split would have been a cycle back through `create_app`.

Validation lives on the models rather than in the handlers on purpose: a bad
`mode`, `permission_mode`, or `scheduled_for` is refused at the edge as a 422,
before any journal write or spawn, and the vocabularies are read from
`launcher` rather than restated so there is one source for each.
"""

from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from bridge import launcher, spool

HandoffStatus = Literal["queued", "consumed", "dismissed", "superseded"]

# Three values, not two. `archived` is what `config.toml` seeds for a directory
# that is gone; `hidden` is what the panel's own control writes; `active`
# restores either. They filter identically in `Store.projects`, which whitelists
# `active` -- the distinction is a record of who decided, and that is what makes
# the seed-versus-override rule in `indexer.reindex` legible.
ProjectStatus = Literal["active", "hidden", "archived"]


class HandoffIn(BaseModel):
    """The CLI mints `id`, which is what makes a re-drained spool file collide
    on the primary key instead of inserting a duplicate."""

    id: str
    project_path: str
    next_prompt: str
    session_id: str | None = None
    summary: str | None = None
    suggested_model: str | None = None
    suggested_effort: str | None = None
    created_at: int | None = None

    @field_validator("id")
    @classmethod
    def _usable_as_a_filename(cls, value: str) -> str:
        # The id becomes the journal file's stem, so a `/` or `..` in it would
        # aim an accepted handoff at a path outside the spool. `spool` refuses
        # it as well -- that is the chokepoint every writer shares -- but
        # `post_handoff` swallows a journal failure on purpose, so without this
        # the POST would 201 with `journaled: false` and leave a row the
        # journal can never rebuild. Here it is a 422 with nothing stored.
        spool.check_record_id(value)
        return value


class HandoffPatch(BaseModel):
    """`status`, `next_prompt`, or both — but never neither.

    Both fields are optional because the panel edits a prompt without touching
    the status and dismisses a handoff without touching its text. Optional fields
    alone, though, make `PATCH {}` a 200 that changes nothing, so the validator
    below rejects the empty body outright: a silent no-op is indistinguishable
    from a saved edit at the far end of a `fetch()`.
    """

    status: HandoffStatus | None = None
    next_prompt: str | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.status is None and self.next_prompt is None:
            raise ValueError("supply status, next_prompt, or both")
        return self


class LaunchIn(BaseModel):
    """`prompt` is optional, and that is the load-bearing part.

    `bridge launch` deliberately sends no prompt: the server already holds the
    queued handoff, so round-tripping it out to the client and back would be
    bytes over the wire — and a second copy to keep in sync — for nothing. When
    it is omitted the server uses that project's queued handoff, and having
    neither is an error rather than an empty session.
    """

    project_path: str
    prompt: str | None = None
    mode: str = "terminal"
    model: str | None = None
    effort: str | None = None
    handoff_id: str | None = None
    title: str | None = None
    # Absent means "ask as usual". Deliberately has no server-side memory: the
    # panel re-sends it per launch, so a dangerous mode can never carry over.
    permission_mode: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_permission_mode(cls, value: str | None) -> str | None:
        # Rejected at the edge with a 422 rather than deep inside `launch()`,
        # and read from `launcher.PERMISSION_MODES` rather than restated so the
        # vocabulary has one source. "" is the select's default and means none.
        if value and value not in launcher.PERMISSION_MODES:
            raise ValueError(
                f"permission_mode must be one of "
                f"{sorted(launcher.PERMISSION_MODES)}"
            )
        return value

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        # Validated here rather than left to `launch()` so the check does not
        # depend on which launcher is injected, and consumed from `launcher.MODES`
        # rather than restated, so the vocabulary has one source.
        if value not in launcher.MODES:
            raise ValueError(f"mode must be one of {launcher.MODES}")
        return value


def _validate_prompt_field(value: str | None) -> str | None:
    """Shared by `ScheduleIn` and `SchedulePatch`: map `launcher.LaunchError`
    to a Pydantic `ValueError`, so a NUL or oversize prompt is a 422 at the
    edge instead of surfacing later, mid-fire, as an uncaught exception."""
    if value is not None:
        try:
            launcher.validate_prompt(value)
        except launcher.LaunchError as exc:
            raise ValueError(str(exc)) from exc
    return value


def _check_known_mode(value: str) -> str:
    """Shared by `ScheduleIn` and `SchedulePatch`. Checks against the same
    closed set `LaunchIn._known_mode` does, written once here rather than
    copied into both, which is also what keeps the mutation harness's anchor
    into `LaunchIn`'s own check (`tools/mutations/phase3-task4.json`) matching
    exactly once."""
    if value not in launcher.MODES:
        raise ValueError(f"mode must be one of {launcher.MODES}")
    return value


def _check_known_permission_mode(value: str | None) -> str | None:
    """Shared by `ScheduleIn` and `SchedulePatch`; see `_check_known_mode`."""
    if value and value not in launcher.PERMISSION_MODES:
        raise ValueError(
            f"permission_mode must be one of {sorted(launcher.PERMISSION_MODES)}"
        )
    return value


class ScheduleIn(BaseModel):
    """A session to launch at a future time. Mirrors `LaunchIn`'s validators:
    `mode` and `permission_mode` are checked against the same closed sets, and
    `prompt` -- required here, unlike `LaunchIn`, since a scheduled run has no
    running request to fall back to a queued handoff from -- runs through the
    same `validate_prompt` a manual launch would hit at fire time, so a
    doomed-to-fail prompt is refused at scheduling instead of at 3am.
    """

    project_path: str
    prompt: str
    scheduled_for: int
    mode: str = "terminal"
    model: str | None = None
    effort: str | None = None
    summary: str | None = None
    permission_mode: str | None = None
    source_handoff_id: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_permission_mode(cls, value: str | None) -> str | None:
        return _check_known_permission_mode(value)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        return _check_known_mode(value)

    @field_validator("prompt")
    @classmethod
    def _valid_prompt(cls, value: str) -> str:
        return _validate_prompt_field(value)

    @field_validator("scheduled_for")
    @classmethod
    def _sane_epoch(cls, value: int) -> int:
        # Rejects anything outside year-0-to-3000: a value this far outside
        # any real schedule is almost certainly a unit mistake (ms instead of
        # seconds) rather than an intentional far-future run, and letting it
        # through would only surface later as a `datetime.fromtimestamp`
        # crash in the dashboard render.
        if value < 0 or value > 32_503_680_000:
            raise ValueError("scheduled_for must be a sane epoch-seconds value")
        return value


class SchedulePatch(BaseModel):
    """Edits a still-`pending` scheduled run. Every field is optional -- a
    caller edits only what changed -- but `store.edit_pending` turns an empty
    set of fields into an empty `SET` clause, so an empty body is rejected
    the same way `HandoffPatch` and `ProjectPatch` reject theirs.
    """

    prompt: str | None = None
    scheduled_for: int | None = None
    model: str | None = None
    effort: str | None = None
    mode: str | None = None
    summary: str | None = None
    permission_mode: str | None = None

    @field_validator("permission_mode")
    @classmethod
    def _known_permission_mode(cls, value: str | None) -> str | None:
        return _check_known_permission_mode(value)

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str | None) -> str | None:
        return value if value is None else _check_known_mode(value)

    @field_validator("prompt")
    @classmethod
    def _valid_prompt(cls, value: str | None) -> str | None:
        return _validate_prompt_field(value)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("supply at least one field")
        return self

    @model_validator(mode="after")
    def _no_explicit_null_on_required_fields(self):
        # `exclude_unset=True` at the route is what makes an OMITTED field a
        # no-op -- but it cannot tell an omitted field from an EXPLICIT
        # `null` for the same reason: both are simply absent from
        # `model_fields_set` until pydantic sees the key at all, and an
        # explicit `null` *does* set it, with a `None` value. `prompt`,
        # `mode`, and `scheduled_for` back NOT NULL columns, so a `None` that
        # reaches `store.edit_pending` for one of them is a 500, not a no-op.
        for name in ("prompt", "mode", "scheduled_for"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ProjectPatch(BaseModel):
    """`status`, `pinned`, or both -- but never neither.

    Both optional because the panel pins a project without touching its
    visibility and hides one without touching its pin. Optional fields alone,
    though, make `PATCH {}` a 200 that changes nothing, so the validator below
    rejects the empty body: at the far end of a `fetch()` a silent no-op is
    indistinguishable from a saved change. Same shape as `HandoffPatch`, for
    exactly the same reason.
    """

    status: ProjectStatus | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.status is None and self.pinned is None:
            raise ValueError("supply status, pinned, or both")
        return self


class UpdateIn(BaseModel):
    """`POST /api/update`'s body: the exact SHA to install.

    Never a branch name or `@main` -- the route below cross-checks this
    against the checker's own currently-surfaced `latest_sha`, so a request
    can only ever pin the concrete commit the panel already offered.
    """

    target_sha: str

    @field_validator("target_sha")
    @classmethod
    def _forty_hex(cls, v: str) -> str:
        if len(v) != 40 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("target_sha must be a 40-char lowercase hex SHA")
        return v

"""Structured dashboard snapshots shared by refresh and the live stream."""

from __future__ import annotations

import dataclasses
from dataclasses import replace
from typing import Callable

from bridge import agents, hooks, spool
from bridge.cards import FIVE_HOURS, ONE_DAY, build_cards, spark_points
from bridge.config import Config
from bridge.models import AgentsState, Card, GitState
from bridge.refresh import RefreshCoordinator, RefreshResult, RefreshStatus
from bridge.store import Store, now_epoch


class DashboardBuilder:
    def __init__(
        self,
        store: Store,
        cfg: Config,
        coordinator: RefreshCoordinator,
        *,
        debouncer=None,
        hook_state=None,
        probe_fn=None,
        agents_fn=None,
        now_fn: Callable[[], int] = now_epoch,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.coordinator = coordinator
        self.debouncer = debouncer
        self.hook_state = hook_state
        self.probe_fn = probe_fn
        self.agents_fn = agents_fn
        self.now_fn = now_fn

    def full_update(
        self,
        refresh: RefreshResult | None = None,
        *,
        live_state: AgentsState | None = None,
        cards: list[Card] | None = None,
        now: int | None = None,
    ) -> dict:
        now = self.now_fn() if now is None else now
        state = live_state if live_state is not None else self._live_state(now)
        if cards is None:
            cards = build_cards(
                self.store,
                self.cfg,
                probe_fn=self.probe_fn,
                agents_fn=lambda: state,
                debouncer=None,
                hook_state=None,
            )
        status = self.coordinator.status_snapshot()
        return self._envelope(
            kind="snapshot",
            now=now,
            status=status,
            refresh=refresh,
            cards=cards,
            live_state=state,
        )

    def live_patch(self) -> dict:
        now = self.now_fn()
        state = self._live_state(now)
        cards = build_cards(
            self.store,
            self.cfg,
            probe_fn=lambda _path: GitState(status="unavailable"),
            agents_fn=lambda: state,
            debouncer=None,
            hook_state=None,
        )
        status = self.coordinator.status_snapshot()
        payload = self._envelope(
            kind="patch",
            now=now,
            status=status,
            refresh=None,
            cards=cards,
            live_state=state,
        )
        payload.pop("card_order", None)
        for card in payload["cards"].values():
            card.pop("git", None)
            card.pop("burn", None)
        return payload

    def _live_state(self, now: int) -> AgentsState:
        try:
            state = self.agents_fn() if self.agents_fn else agents.probe()
        except Exception:  # noqa: BLE001
            state = AgentsState(status="unavailable", sessions=[], source="none")
        if self.hook_state is not None and state.status == "ok":
            self.hook_state.forget(s.session_id for s in state.sessions)
            waiting = self.hook_state.waiting_ids()
            if waiting:
                state = replace(state, sessions=[
                    replace(s, status=hooks.NEEDS_INPUT)
                    if s.session_id in waiting else s
                    for s in state.sessions
                ])
        if self.debouncer is not None:
            state = replace(state, sessions=self.debouncer.apply(state.sessions, now))
        return state

    def _envelope(
        self,
        *,
        kind: str,
        now: int,
        status: RefreshStatus,
        refresh: RefreshResult | None,
        cards: list[Card],
        live_state: AgentsState,
    ) -> dict:
        latest = self.store.latest_index_run()
        index_at = status.index_at
        if index_at is None and latest is not None:
            index_at = int(latest["ran_at"])
        age = max(0, now - index_at) if index_at is not None else None
        parse_errors = int(latest["parse_errors"] or 0) if latest is not None else 0
        unavailable = status.server == "unavailable"
        running = sum(1 for s in live_state.sessions if not agents.is_terminal(s.status))
        card_data = {str(card.project_id): _card_update(card) for card in cards}
        refresh_payload = {
            "attempted": refresh is not None,
            "completed": refresh.completed if refresh is not None else True,
            "stats": dataclasses.asdict(refresh.stats) if refresh and refresh.stats else None,
            "error": refresh.error if refresh else status.error,
        }
        return {
            "schema": 1,
            "kind": kind,
            "generated_at": now,
            "generation": status.generation,
            "refresh": refresh_payload,
            "freshness": {
                "server": "unavailable" if unavailable else "available",
                "index_at": index_at,
                "index_age_seconds": age,
            },
            "topbar": {
                "projects": len(cards),
                "running": running,
                "queued": self.store.queued_handoff_count(),
                "scheduled": sum(
                    1 for row in self.store.scheduled_runs()
                    if row["status"] in ("pending", "launching")
                ),
                "today": sum(card.tokens_today for card in cards),
                "last_5h": sum(card.tokens_5h for card in cards),
                "burn_rate": sum(card.tokens_5h for card in cards) // (FIVE_HOURS // 3600),
                "last_index": index_at,
            },
            "diagnostics": {
                "alert": bool(parse_errors or spool.pending_count(self.cfg.spool_dir)
                              or live_state.status == "unavailable"),
            },
            "card_order": [card.project_id for card in cards],
            "cards": card_data,
            "unattributed": _unattributed(live_state, self.store),
        }


def _card_update(card: Card) -> dict:
    live = card.live
    return {
        "live": {
            "available": live is not None,
            "status": live.status if live is not None else "ended",
            "started_at": live.started_at if live is not None else None,
            "model": card.session.model if card.session else None,
            "effort": card.session.effort if card.session else None,
        },
        "git": {
            "status": card.git.status,
            "branch": card.git.branch,
            "dirty_count": card.git.dirty_count,
            "ahead": card.git.ahead,
            "behind": card.git.behind,
            "oldest_uncommitted_at": card.git.oldest_uncommitted_at,
            "cached_at": card.git.cached_at,
            "stale": card.is_stale,
        },
        "burn": {
            "today": card.tokens_today,
            "last_5h": card.tokens_5h,
            "spark_points": spark_points(card.spark),
        },
    }


def _unattributed(state: AgentsState, store: Store) -> list[dict]:
    paths = {row["path"] for row in store.projects()}
    out = []
    for session in state.sessions:
        if session.cwd not in paths:
            out.append({"path": session.cwd, "status": session.status,
                        "started_at": session.started_at})
    return out

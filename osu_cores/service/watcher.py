import logging
import time
from collections import defaultdict
from datetime import datetime, timezone

from ..api.client import OsuClient
from ..mapper import map_match
from ..models import Match, MatchEvent, Player

log = logging.getLogger(__name__)
PAGE_SIZE = 100
# Un mapa sin terminar que no es el actual y empezó hace más que esto se
# considera abandonado. Es una heurística: el valor no está medido contra la API.
STALE_GAME_SECONDS = 30 * 60


def _age_seconds(iso: str) -> float:
    started = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - started).total_seconds()


class MatchWatcher:
    def __init__(self, client: OsuClient, match_id: int):
        self.client = client
        self.match_id = match_id
        self.players: dict[int, Player] = {}
        self.ended = False
        self.cursor: int | None = None
        self._seen: set[int] = set()
        self._started: set[int] = set()
        self._finished: set[int] = set()
        self._handlers: dict[str, list] = defaultdict(list)

    # --- suscripción ---------------------------------------------------
    def on(self, name: str, handler):
        """Nombres: backfill, game_started, game_finished, event, match_ended."""
        self._handlers[name].append(handler)
        return handler

    def _emit(self, name: str, *args):
        for handler in self._handlers[name]:
            try:
                handler(*args)
            except Exception:
                log.exception("Falló un handler de %r", name)

    # --- backfill ------------------------------------------------------
    def backfill(self, max_pages: int = 500, pause: float = 0.3) -> Match:
        first = self.client.get_match(self.match_id)
        events = {e["id"]: e for e in first["events"]}
        users = {u["id"]: u for u in first["users"]}
        oldest_wanted = first["first_event_id"]

        for _ in range(max_pages):
            if not events or min(events) <= oldest_wanted:
                break
            time.sleep(pause)
            page = self.client.get_match(self.match_id, before=min(events))
            fresh = [e for e in page["events"] if e["id"] not in events]
            if not fresh:
                break
            for e in fresh:
                events[e["id"]] = e
            for u in page["users"]:
                users[u["id"]] = u

        raw = dict(first)
        raw["events"] = [events[i] for i in sorted(events)]
        raw["users"] = list(users.values())
        match = map_match(raw)

        self._update_players(match.players)
        self._process(match.events, emit=False)
        self._advance(match.events, match.current_game_id)
        return match

    # --- polling -------------------------------------------------------
    def poll_once(self) -> int:
        if self.cursor is None:
            raise RuntimeError("Llamá a backfill() antes de poll_once()")

        after = self.cursor
        collected: list[MatchEvent] = []
        current = None
        for _ in range(50):
            match = map_match(self.client.get_match(self.match_id, after=after))
            current = match.current_game_id
            self._update_players(match.players)
            collected.extend(match.events)
            if len(match.events) < PAGE_SIZE:
                break
            after = max(e.id for e in match.events)

        events = sorted(collected, key=lambda e: e.id)
        self._process(events)
        self._advance(events, current)
        return len(events)

    def run(self, interval: float = 5.0):
        match = self.backfill()
        self._emit("backfill", match)
        try:
            while not self.ended:
                time.sleep(interval)
                self.poll_once()
        except KeyboardInterrupt:
            pass

    # --- internos ------------------------------------------------------
    def _update_players(self, players):
        self.players.update({p.id: p for p in players})

    def _process(self, events: list[MatchEvent], emit: bool = True):
        for e in events:
            if e.type == "game":
                g = e.game
                if g.id not in self._started:
                    self._started.add(g.id)
                    if emit:
                        self._emit("game_started", g)
                if g.is_finished and g.id not in self._finished:
                    self._finished.add(g.id)
                    self._seen.add(e.id)
                    if emit:
                        self._emit("game_finished", g)
                        self._emit("event", e)
                continue

            if e.id in self._seen:
                continue
            self._seen.add(e.id)
            if e.type == "match-disbanded":
                self.ended = True
            if emit:
                self._emit("event", e)
                if e.type == "match-disbanded":
                    self._emit("match_ended", e)

    def _advance(self, events: list[MatchEvent], current_game_id: int | None = None):
        """Mueve el cursor. Se queda antes del mapa que sigue en juego (el
        actual, o uno reciente) para volver a pedirlo hasta que termine. Los
        sin terminar que son viejos se tratan como abandonados."""
        if not events:
            return
        pending = [
            e.id for e in events
            if e.type == "game"
            and not e.game.is_finished
            and (e.game.id == current_game_id
                 or _age_seconds(e.game.start_time) < STALE_GAME_SECONDS)
        ]
        self.cursor = (min(pending) - 1) if pending else max(e.id for e in events)

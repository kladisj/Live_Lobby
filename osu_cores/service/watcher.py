import time

from ..api.client import OsuClient
from ..mapper import map_match
from ..models import Match


class MatchWatcher:
    def __init__(self, client: OsuClient, match_id: int):
        self.client = client
        self.match_id = match_id
        self.last_event_id: int | None = None  # lo usará el polling (parte B)

    def backfill(self, max_pages: int = 500, pause: float = 0.3) -> Match:
        """Baja el historial completo paginando hacia atrás con `before`."""
        first = self.client.get_match(self.match_id)  # los últimos 100 eventos
        events = {e["id"]: e for e in first["events"]}
        users = {u["id"]: u for u in first["users"]}
        oldest_wanted = first["first_event_id"]

        for _ in range(max_pages):
            if not events or min(events) <= oldest_wanted:
                break  # ya llegamos al principio
            time.sleep(pause)
            page = self.client.get_match(self.match_id, before=min(events))
            fresh = [e for e in page["events"] if e["id"] not in events]
            if not fresh:
                break  # la API no devolvió nada nuevo: evita un loop infinito
            for e in fresh:
                events[e["id"]] = e
            for u in page["users"]:
                users[u["id"]] = u

        raw = dict(first)
        raw["events"] = [events[i] for i in sorted(events)]
        raw["users"] = list(users.values())
        self.last_event_id = max(events) if events else None
        return map_match(raw)

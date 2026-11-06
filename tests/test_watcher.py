from osu_cores.service.watcher import MatchWatcher


def make_event(i):
    return {"id": i, "detail": {"type": "player-joined"},
            "timestamp": "2026-01-01T00:00:00Z", "user_id": 1}


class FakeClient:
    """Imita a la API: devuelve hasta 100 eventos y pagina con `before`."""

    def __init__(self, ids):
        self.ids = sorted(ids)
        self.calls = 0

    def get_match(self, match_id, after=None, before=None, limit=None):
        self.calls += 1
        pool = [i for i in self.ids if before is None or i < before]
        return {
            "match": {"id": match_id, "name": "fake"},
            "events": [make_event(i) for i in pool[-100:]],
            "users": [{"id": 1, "username": "a"}],
            "first_event_id": self.ids[0],
            "latest_event_id": self.ids[-1],
            "current_game_id": None,
        }


class StuckClient:
    """API que ignora `before` y siempre devuelve la misma página."""

    def get_match(self, match_id, after=None, before=None, limit=None):
        return {
            "match": {"id": match_id, "name": "fake"},
            "events": [make_event(i) for i in range(100, 200)],
            "users": [{"id": 1, "username": "a"}],
            "first_event_id": 1,
            "latest_event_id": 199,
            "current_game_id": None,
        }


def test_backfill_pagina_todo():
    client = FakeClient(range(1000, 1500, 2))  # 250 eventos, IDs no consecutivos
    match = MatchWatcher(client, 1).backfill(pause=0)
    ids = [e.id for e in match.events]
    assert len(ids) == 250
    assert ids == sorted(set(ids))
    assert client.calls == 3


def test_backfill_partida_corta_una_sola_llamada():
    client = FakeClient(range(30))
    match = MatchWatcher(client, 1).backfill(pause=0)
    assert len(match.events) == 30
    assert client.calls == 1


def test_backfill_no_entra_en_loop_infinito():
    match = MatchWatcher(StuckClient(), 1).backfill(pause=0)
    assert len(match.events) == 100

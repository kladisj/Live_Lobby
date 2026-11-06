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

T = "2026-01-01T00:00:00Z"

def ev(i, type_="player-joined"):
    return {"id": i, "detail": {"type": type_}, "timestamp": T, "user_id": 1}


def game_ev(i, game_id, finished):
    return {
        "id": i, "detail": {"type": "other"}, "timestamp": T, "user_id": None,
        "game": {
            "id": game_id, "match_id": 1, "beatmap": None, "start_time": "2999-01-01T00:00:00Z",
            "end_time": T if finished else None, "mode": "osu",
            "scoring_type": "scorev2", "team_type": "team-vs", "mods": [], "scores": [],
        },
    }


def raw_of(events):
    return {
        "match": {"id": 1, "name": "fake"}, "events": events,
        "users": [{"id": 1, "username": "a"}],
        "first_event_id": 1, "latest_event_id": 99, "current_game_id": None,
    }


class ScriptedClient:
    """Devuelve las respuestas en orden y anota con qué parámetros lo llamaron."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_match(self, match_id, after=None, before=None, limit=None):
        self.calls.append((after, before))
        return self.responses.pop(0)


def test_polling_mapa_en_curso_se_emite_una_sola_vez():
    client = ScriptedClient([
        raw_of([ev(1, "match-created")]),                          # backfill
        raw_of([game_ev(2, 100, False)]),                          # mapa empieza
        raw_of([game_ev(2, 100, False), ev(3)]),                   # sigue + entra alguien
        raw_of([game_ev(2, 100, True), ev(3)]),                    # termina
    ])
    w = MatchWatcher(client, 1)
    started, finished, events = [], [], []
    w.on("game_started", lambda g: started.append(g.id))
    w.on("game_finished", lambda g: finished.append(g.id))
    w.on("event", lambda e: events.append(e.id))

    w.backfill(pause=0)
    for _ in range(3):
        w.poll_once()

    assert started == [100]
    assert finished == [100]
    assert events.count(3) == 1  # el jugador no se emite repetido
    assert client.calls == [(None, None), (1, None), (1, None), (1, None)]
    assert w.cursor == 3


def test_run_termina_cuando_se_cierra_la_partida():
    client = ScriptedClient([
        raw_of([ev(1, "match-created")]),
        raw_of([ev(2, "match-disbanded")]),
    ])
    w = MatchWatcher(client, 1)
    ended = []
    w.on("match_ended", lambda e: ended.append(e.id))

    w.run(interval=0)

    assert w.ended
    assert ended == [2]


def test_error_en_un_handler_no_mata_al_watcher():
    client = ScriptedClient([raw_of([ev(1, "match-created")]), raw_of([ev(2), ev(3)])])
    w = MatchWatcher(client, 1)
    ok = []

    def malo(e):
        raise ValueError("boom")

    w.on("event", malo)
    w.on("event", lambda e: ok.append(e.id))

    w.backfill(pause=0)
    w.poll_once()
    assert ok == [2, 3]


def test_mapa_abandonado_no_traba_el_cursor():
    viejo = game_ev(2, 100, False)
    viejo["game"]["start_time"] = "2020-01-01T00:00:00Z"
    client = ScriptedClient([
        raw_of([ev(1, "match-created")]),
        raw_of([viejo, ev(3)]),
    ])
    w = MatchWatcher(client, 1)
    w.backfill(pause=0)
    w.poll_once()
    assert w.cursor == 3  # avanzó: no se quedó esperando al abandonado


def test_mapa_actual_traba_el_cursor_aunque_sea_viejo():
    viejo = game_ev(2, 100, False)
    viejo["game"]["start_time"] = "2020-01-01T00:00:00Z"
    resp = raw_of([viejo, ev(3)])
    resp["current_game_id"] = 100
    client = ScriptedClient([raw_of([ev(1, "match-created")]), resp])
    w = MatchWatcher(client, 1)
    w.backfill(pause=0)
    w.poll_once()
    assert w.cursor == 1  # lo retiene porque current_game_id lo señala

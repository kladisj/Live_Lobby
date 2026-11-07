import json

import pytest

from osu_cores.models import Game, MatchEvent, Player
from osu_cores.output.jsonl import SCHEMA_VERSION, JsonlWriter

T = "2026-01-01T00:00:00Z"


class FakeWatcher:
    """Lo mínimo que usa JsonlWriter: match_id, players y on()."""

    def __init__(self, match_id=1):
        self.match_id = match_id
        self.players = {}
        self.handlers = {}

    def on(self, name, handler):
        self.handlers[name] = handler

    def emit(self, name, *args):
        self.handlers[name](*args)


class FakeMatch:
    def __init__(self, events):
        self.events = events


def event(i, type_="player-joined"):
    return MatchEvent(id=i, type=type_, timestamp=T, user_id=1)


def game_event(i, finished):
    game = Game(
        id=100 + i, match_id=1, beatmap=None, start_time=T,
        end_time=T if finished else None, mode="osu", scoring_type="scorev2",
        team_type="team-vs",
    )
    return MatchEvent(id=i, type="game", timestamp=T, game=game)


def read(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def events_in(path):
    return [r["event"]["id"] for r in read(path) if r["kind"] == "event"]


def test_escribe_jugadores_antes_que_eventos_con_version(tmp_path):
    path = tmp_path / "p.jsonl"
    w = FakeWatcher()
    w.players = {7: Player(id=7, username="ana", country_code="AR")}
    JsonlWriter(path, w)

    w.emit("event", event(1))

    recs = read(path)
    assert [r["kind"] for r in recs] == ["player", "event"]
    assert all(r["schema_version"] == SCHEMA_VERSION for r in recs)
    assert all(r["match_id"] == 1 for r in recs)
    assert recs[0]["player"]["username"] == "ana"


def test_no_repite_el_mismo_evento(tmp_path):
    path = tmp_path / "p.jsonl"
    w = FakeWatcher()
    JsonlWriter(path, w)

    w.emit("event", event(1))
    w.emit("event", event(1))

    assert events_in(path) == [1]


def test_backfill_escribe_el_historial_ordenado(tmp_path):
    path = tmp_path / "p.jsonl"
    w = FakeWatcher()
    JsonlWriter(path, w)

    w.emit("backfill", FakeMatch([event(3), event(1), event(2)]))

    assert events_in(path) == [1, 2, 3]


def test_reanudar_agrega_solo_lo_que_falta(tmp_path):
    path = tmp_path / "p.jsonl"
    w1 = FakeWatcher()
    JsonlWriter(path, w1)
    w1.emit("backfill", FakeMatch([event(1), event(2)]))

    # el programa se corta; mientras tanto ocurre el evento 3
    w2 = FakeWatcher()
    JsonlWriter(path, w2)
    w2.emit("backfill", FakeMatch([event(1), event(2), event(3)]))

    assert events_in(path) == [1, 2, 3]


def test_mapa_en_curso_se_escribe_al_terminar(tmp_path):
    path = tmp_path / "p.jsonl"
    w = FakeWatcher()
    JsonlWriter(path, w)

    w.emit("backfill", FakeMatch([event(1), game_event(2, finished=False), event(3)]))
    assert events_in(path) == [1, 3]  # el mapa sin terminar no se escribe

    w.emit("event", game_event(2, finished=True))
    assert events_in(path) == [1, 3, 2]  # aparece al terminar, aunque su id sea menor


def test_mapa_que_termino_mientras_el_programa_estaba_cortado(tmp_path):
    path = tmp_path / "p.jsonl"
    w1 = FakeWatcher()
    JsonlWriter(path, w1)
    w1.emit("backfill", FakeMatch([event(1), game_event(2, finished=False), event(3)]))

    w2 = FakeWatcher()
    JsonlWriter(path, w2)
    w2.emit("backfill", FakeMatch([event(1), game_event(2, finished=True), event(3)]))

    assert events_in(path) == [1, 3, 2]


def test_linea_cortada_no_rompe_el_archivo(tmp_path):
    path = tmp_path / "p.jsonl"
    w1 = FakeWatcher()
    JsonlWriter(path, w1)
    w1.emit("event", event(1))
    with open(path, "a", encoding="utf-8") as f:
        f.write('{"schema_version": 1, "match_id": 1, "kind": "ev')  # cortada

    w2 = FakeWatcher()
    JsonlWriter(path, w2)
    w2.emit("event", event(2))

    valid = []
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            try:
                valid.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # la línea cortada
    assert [r["event"]["id"] for r in valid if r["kind"] == "event"] == [1, 2]


def test_rechaza_archivo_de_otra_partida(tmp_path):
    path = tmp_path / "p.jsonl"
    w1 = FakeWatcher(match_id=1)
    JsonlWriter(path, w1)
    w1.emit("event", event(1))

    with pytest.raises(ValueError):
        JsonlWriter(path, FakeWatcher(match_id=2))


def test_acentos_se_guardan_tal_cual(tmp_path):
    path = tmp_path / "p.jsonl"
    w = FakeWatcher()
    w.players = {1: Player(id=1, username="Julián")}
    JsonlWriter(path, w)

    w.emit("event", event(1))

    assert "Julián" in path.read_text(encoding="utf-8")

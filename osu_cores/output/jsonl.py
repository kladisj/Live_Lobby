import json
import logging
import os

from ..models import MatchEvent, to_dict

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class JsonlWriter:
    """Escribe en un archivo JSONL lo que detecta un MatchWatcher.

    Una línea por registro, de tres clases:
      {"schema_version": 1, "match_id": ..., "kind": "match",  "match":  {...}}
      {"schema_version": 1, "match_id": ..., "kind": "player", "player": {...}}
      {"schema_version": 1, "match_id": ..., "kind": "event",  "event":  {...}}

    - El registro "match" (nombre, hora de inicio y de cierre de la partida) se
      escribe una sola vez. Si el archivo es viejo y no lo tiene, se agrega la
      próxima vez que se siga la misma partida con ese archivo. "end_time" puede
      ser null: para saber si la sala cerró, buscá el evento "match-disbanded".

    - El archivo es un registro completo de la partida: la primera vez incluye
      el historial; si se reanuda, agrega solo lo que falte.
    - Las líneas van en el orden en que se detectaron, no ordenadas por id.
      Un mapa aparece cuando termina (con sus scores), no cuando empieza.
    - Se usa con watcher.run(): el historial llega por el callback "backfill".
    """

    def __init__(self, path, watcher):
        self.path = os.fspath(path)
        self.watcher = watcher
        self.match_id = watcher.match_id
        self._event_ids: set[int] = set()
        self._player_ids: set[int] = set()
        self._has_match_info = False
        self._load_existing()
        watcher.on("backfill", self._on_backfill)
        watcher.on("event", self._on_event)

    # --- reanudación ---------------------------------------------------
    def _load_existing(self):
        """Lee lo que ya hay en el archivo para no escribirlo dos veces."""
        if not os.path.exists(self.path):
            return
        with open(self.path, "rb") as f:
            data = f.read()
        if data and not data.endswith(b"\n"):
            # quedó una línea cortada (corte de luz, kill -9): la cerramos
            with open(self.path, "ab") as f:
                f.write(b"\n")

        text = data.decode("utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("%s: la línea %d es ilegible, se ignora", self.path, n)
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("match_id") != self.match_id:
                raise ValueError(
                    f"{self.path} tiene datos de la partida {rec.get('match_id')}, "
                    f"no de la {self.match_id}"
                )
            if rec.get("kind") == "event":
                self._event_ids.add(rec["event"]["id"])
            elif rec.get("kind") == "player":
                self._player_ids.add(rec["player"]["id"])
            elif rec.get("kind") == "match":
                self._has_match_info = True

    # --- escritura -----------------------------------------------------
    def _write(self, record: dict):
        record = {"schema_version": SCHEMA_VERSION, "match_id": self.match_id, **record}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_match_info(self, match):
        if self._has_match_info:
            return
        self._has_match_info = True
        self._write({"kind": "match", "match": {
            "name": match.name,
            "start_time": match.start_time,
            "end_time": match.end_time,
        }})

    def _write_new_players(self):
        for p in self.watcher.players.values():
            if p.id not in self._player_ids:
                self._player_ids.add(p.id)
                self._write({"kind": "player", "player": to_dict(p)})

    def _write_event(self, e: MatchEvent):
        if e.id in self._event_ids:
            return
        if e.game is not None and not e.game.is_finished:
            # El evento de un mapa conserva su id al terminar, así que se
            # escribe recién cuando termina, con sus scores.
            return
        self._write_new_players()  # los jugadores van antes de lo que los usa
        self._event_ids.add(e.id)
        self._write({"kind": "event", "event": to_dict(e)})

    # --- callbacks del watcher ----------------------------------------
    def _on_backfill(self, match):
        self._write_match_info(match)
        for e in sorted(match.events, key=lambda e: e.id):
            self._write_event(e)

    def _on_event(self, e: MatchEvent):
        self._write_event(e)

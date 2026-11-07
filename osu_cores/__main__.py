"""Uso: python -m osu_cores <id_o_link_del_mp> [--out partida.jsonl] [--interval 5]"""

import argparse
import logging
import re
import sys
import time

from .api.client import OsuApiError, OsuClient
from .output.jsonl import JsonlWriter
from .service.watcher import MatchWatcher


def say(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_match(value: str) -> int:
    """Acepta el ID suelto o un mp link (…/community/matches/123, …/mp/123)."""
    m = re.search(r"(\d+)/?\s*$", value)
    if not m:
        raise argparse.ArgumentTypeError(f"no encontré un ID de partida en {value!r}")
    return int(m.group(1))


def title_of(game) -> str:
    b = game.beatmap
    if b and b.title:
        return f"{b.title} [{b.version}]" if b.version else b.title
    return f"mapa {game.id}"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="osu_cores",
        description="Sigue una partida multijugador de osu! y, si querés, la guarda en JSONL.",
    )
    p.add_argument("match", type=parse_match, help="ID o link (mp link) de la partida")
    p.add_argument("--out", help="archivo JSONL donde guardar los datos (opcional)")
    p.add_argument("--interval", type=float, default=5.0,
                   help="segundos entre consultas (por defecto 5)")
    args = p.parse_args(argv)
    if args.interval < 1:
        p.error("--interval tiene que ser de al menos 1 segundo")

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    try:
        client = OsuClient()
    except KeyError as e:
        print(f"Falta la variable {e.args[0]} (revisá tu .env)", file=sys.stderr)
        return 2

    w = MatchWatcher(client, args.match)

    if args.out:
        try:
            JsonlWriter(args.out, w)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2

    def name_of(user_id):
        player = w.players.get(user_id)
        return player.username if player else str(user_id)

    def on_backfill(match):
        say(f"historial: {len(match.events)} eventos, {len(match.games)} mapas, "
            f"{len(match.players)} jugadores")

    def on_started(game):
        say(f"empezó {title_of(game)}")

    def on_finished(game):
        best = max(game.scores, key=lambda s: s.score, default=None)
        extra = f" | mejor: {name_of(best.user_id)} ({best.score:,})" if best else ""
        say(f"terminó {title_of(game)} | {len(game.scores)} scores{extra}")

    w.on("backfill", on_backfill)
    w.on("game_started", on_started)
    w.on("game_finished", on_finished)

    say(f"siguiendo la partida {args.match} (Ctrl+C para cortar)")
    try:
        w.run(interval=args.interval)
    except OsuApiError as e:
        print(f"Error de la API: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        say("cortado")
        return 130

    if w.ended:
        say("la partida está cerrada")
    return 0


if __name__ == "__main__":
    sys.exit(main())

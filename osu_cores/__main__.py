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


TEAM_LABEL = {"red": "rojo", "blue": "azul"}
TEAM_TYPES = ("team-vs", "tag-team-vs")


def _score_line(rank, name, s) -> str:
    mark = "" if s.passed else "  (falló)"
    return f"    {rank}. {name:<16} {s.score:>12,}  {s.accuracy * 100:6.2f}%{mark}"


def _team_totals(teams, only_passed):
    return {
        t: sum(s.score for s in ss if s.passed or not only_passed)
        for t, ss in teams.items()
    }


def _winner(totals):
    """Equipo con más score, o None si hay empate o falta un equipo."""
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    if len(ranked) < 2 or ranked[0][1] == ranked[1][1]:
        return None
    return ranked[0][0]


def format_result(game, name_of) -> list[str]:
    """Líneas con el resultado de un mapa terminado: quién ganó y el score y la
    accuracy de cada jugador.

    En partidas por equipos, el total de cada equipo suma solo a quienes
    pasaron el mapa; si contar a los que fallaron cambia el resultado, avisa.
    """
    scores = sorted(game.scores, key=lambda s: s.score, reverse=True)
    if not scores:
        return ["  sin scores"]

    lines = []
    if game.team_type in TEAM_TYPES and all(s.team in TEAM_LABEL for s in scores):
        teams = {}
        for s in scores:
            teams.setdefault(s.team, []).append(s)

        totals = _team_totals(teams, only_passed=True)
        winner = _winner(totals)
        if winner:
            other = next(t for t in totals if t != winner)
            diff = totals[winner] - totals[other]
            lines.append(
                f"  GANÓ {TEAM_LABEL[winner].upper()}: "
                f"{totals[winner]:,} vs {totals[other]:,} (+{diff:,})"
            )
        else:
            lines.append("  EMPATE (o falta un equipo)")

        winner_all = _winner(_team_totals(teams, only_passed=False))
        if winner_all != winner:
            alt = f"ganaría {TEAM_LABEL[winner_all]}" if winner_all else "empatarían"
            lines.append(f"  (ojo: si contaran los que fallaron, {alt})")

        for team in sorted(totals, key=lambda t: totals[t], reverse=True):
            lines.append(f"  {TEAM_LABEL[team]} ({totals[team]:,})")
            for i, s in enumerate(teams[team], 1):
                lines.append(_score_line(i, name_of(s.user_id), s))
    else:
        top = scores[0]
        lines.append(f"  GANÓ {name_of(top.user_id)}: {top.score:,}")
        for i, s in enumerate(scores, 1):
            lines.append(_score_line(i, name_of(s.user_id), s))
    return lines


EPILOG = """\
ejemplos:
  python -m osu_cores 122095688
      muestra el historial de la partida y la sigue en vivo
  python -m osu_cores 122095688 --out partida.jsonl
      además guarda los datos en un archivo JSONL
  python -m osu_cores https://osu.ppy.sh/community/matches/122095688
      también acepta el mp link completo
  python -m osu_cores 122095688 --out partida.jsonl --interval 10
      consulta cada 10 segundos en lugar de cada 5

cómo funciona:
  - al empezar baja todo el historial de la partida; en salas muy viejas
    puede tardar un minuto
  - después consulta cada --interval segundos y avisa cuando un mapa
    empieza; cuando termina, muestra quién ganó y el score y la
    accuracy de cada jugador
  - se detiene sola cuando la partida se cierra, o con Ctrl+C
  - con --out, cada mapa se guarda al terminar, con sus scores; volver a
    correr el mismo comando con el mismo archivo no repite lo ya guardado

requisitos:
  un archivo .env con OSU_CLIENT_ID y OSU_CLIENT_SECRET

códigos de salida:
  0 todo bien | 1 error de la API | 2 uso o configuración inválidos
  130 cortado con Ctrl+C
"""


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="osu_cores",
        usage="python -m osu_cores ID_O_LINK [--out ARCHIVO] [--interval SEGUNDOS]",
        description="Sigue una partida multijugador de osu! (mp link) y, si querés, "
                    "la guarda en un archivo JSONL.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    args_group = p.add_argument_group("argumentos")
    args_group.add_argument("match", metavar="ID_O_LINK", type=parse_match,
                            help="ID o link (mp link) de la partida")
    opts = p.add_argument_group("opciones")
    opts.add_argument("--out", metavar="ARCHIVO",
                      help="archivo JSONL donde guardar los datos (opcional)")
    opts.add_argument("--interval", metavar="SEGUNDOS", type=float, default=5.0,
                      help="segundos entre consultas (por defecto 5, mínimo 1)")
    opts.add_argument("-h", "--help", action="help", help="muestra esta ayuda y sale")

    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        p.print_help()  # sin argumentos: mostrar la ayuda en vez de un error
        return 0

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
        say(f"terminó {title_of(game)} | {len(game.scores)} scores")
        for line in format_result(game, name_of):
            print(line, flush=True)

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

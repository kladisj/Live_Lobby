"""Interfaz gráfica local para livelobby (osu_cores) y osuctools.

Uso:  python osu_gui.py        (se abre solo en http://127.0.0.1:8765)

Pone este archivo en una carpeta que contenga las dos carpetas de los repos
(Live_Lobby-main y o-tools-main), o en la raíz de uno de ellos.
Necesita: requests y python-dotenv (los de livelobby).
"""
import contextlib
from dataclasses import asdict
import json
import logging
import os
import re
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
PORT = 8765

# --- encontrar los paquetes ---------------------------------------------------
# Busca osu_cores y osuctools en: esta carpeta, sus subcarpetas (hasta 2 niveles),
# la carpeta de arriba y las hermanas de esta (también hasta 2 niveles). Así
# funciona tanto si el archivo está en una carpeta con los dos repos, como dentro
# de uno de ellos, o si el zip se descomprimió con una carpeta de más.
_SKIP = {".git", ".venv", "venv", "__pycache__", "node_modules", "tests", "examples"}


def _subdirs(folder):
    try:
        return [d for d in sorted(folder.iterdir())
                if d.is_dir() and not d.name.startswith(".") and d.name not in _SKIP]
    except OSError:
        return []


def _search_roots():
    seen, out = set(), []
    for top in (HERE, HERE.parent):
        level1 = _subdirs(top)
        for folder in [top, *level1, *(d for c in level1 for d in _subdirs(c))]:
            if folder not in seen:
                seen.add(folder)
                out.append(folder)
    return out


for base in _search_roots():
    if (base / "osu_cores").is_dir() or (base / "osuctools").is_dir():
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))

MISSING = []


def _hint(nombre, e):
    """Mensaje claro según la causa del fallo al importar."""
    faltante = getattr(e, "name", None) or ""
    if faltante in ("osu_cores", "osuctools"):
        return (f"{nombre}: no encontré la carpeta del repo. Poné este archivo en la misma "
                f"carpeta que Live_Lobby-main y o-tools-main (o dentro de una de las dos).")
    if faltante:
        return (f"{nombre}: falta instalar '{faltante}'. Cerrá esto y corré: "
                f"{Path(sys.executable).name} -m pip install requests python-dotenv")
    return f"{nombre}: {e}"


try:
    from osu_cores.__main__ import format_result, parse_match, title_of
    from osu_cores.api.client import OsuClient
    from osu_cores.output.jsonl import JsonlWriter
    from osu_cores.service.watcher import MatchWatcher
except Exception as e:  # noqa: BLE001
    MISSING.append(_hint("livelobby (osu_cores)", e))
try:
    from osuctools.reader import load
    from osuctools.salidas.consola import format_clean, format_hardest, format_header, format_mvp, format_torneo
    from osuctools.torneo.resumen import summarize, team_labels, trim
    from osuctools.stats import clean_players, hardest_maps, match_costs, team_series
except Exception as e:  # noqa: BLE001
    MISSING.append(_hint("osuctools", e))

# --- seguimiento en vivo (hilo en segundo plano) -------------------------------
W = {"thread": None, "stop": threading.Event(), "log": []}


def log(msg):
    W["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")


class _GuiLog(logging.Handler):
    """Muestra en el registro de la interfaz los avisos de livelobby (por ejemplo,
    si no puede escribir el archivo), que si no quedarían ocultos."""

    def emit(self, record):
        msg = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            msg += f": {record.exc_info[1]}"
        log(f"AVISO: {msg}")


logging.getLogger("osu_cores").addHandler(_GuiLog(level=logging.WARNING))


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.msgs = []

    def emit(self, record):
        self.msgs.append(record.getMessage())


@contextlib.contextmanager
def _warnings_of(name="osuctools"):
    """Junta los avisos del lector (por ejemplo, una corrección que no se pudo aplicar)."""
    handler, logger = _Collect(), logging.getLogger(name)
    logger.addHandler(handler)
    try:
        yield handler.msgs
    finally:
        logger.removeHandler(handler)


def _relevant(warns, match_ids):
    """Un archivo de correcciones suele cubrir varias partidas: se descartan los
    avisos de las que no se cargaron, que no son un problema."""
    keep = []
    for w in dict.fromkeys(warns):
        m = re.search(r"apunta a la partida (\d+)", w)
        if m and int(m.group(1)) not in match_ids:
            continue
        keep.append(w)
    return keep


def _with_warnings(text, warns):
    warns = list(dict.fromkeys(warns))
    if warns:
        text += f"\n\nAvisos ({len(warns)}):\n" + "\n".join(f"- {w}" for w in warns)
    return {"text": text}


def watch_job(match, out, interval, cid, secret):
    try:
        w = MatchWatcher(OsuClient(cid or None, secret or None), match)
        if out:
            out = str(Path(out).expanduser())
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            JsonlWriter(out, w)
        name_of = lambda uid: w.players[uid].username if uid in w.players else str(uid)
        w.on("backfill", lambda m: log(f"historial: {len(m.events)} eventos, "
                                       f"{len(m.games)} mapas, {len(m.players)} jugadores"))
        w.on("game_started", lambda g: log(f"empezó {title_of(g)}"))

        def finished(g):
            log(f"terminó {title_of(g)} | {len(g.scores)} scores")
            W["log"].extend(format_result(g, name_of))
        w.on("game_finished", finished)

        log(f"siguiendo la partida {match}" + (f" -> {out}" if out else ""))
        m = w.backfill()
        w._emit("backfill", m)
        while not W["stop"].is_set() and not w.ended:
            if W["stop"].wait(interval):
                break
            w.poll_once()
        if w.ended:  # tanto si se cerró mientras se seguía como si ya estaba cerrada
            log("la partida está cerrada")
    except KeyError as e:
        log(f"ERROR: falta {e.args[0]} (completá las credenciales o el .env)")
    except Exception as e:  # noqa: BLE001
        log(f"ERROR: {e}")
    log("detenido")


def _falta(prefijo):
    """Si el módulo no se pudo cargar al arrancar, devuelve el motivo (para mostrarlo)."""
    for m in MISSING:
        if m.startswith(prefijo):
            return {"error": m}
    return None


def api_watch_start(d):
    if (r := _falta("livelobby")):
        return r
    if W["thread"] and W["thread"].is_alive():
        return {"error": "ya hay una partida siguiéndose"}
    try:
        match = parse_match(str(d.get("match", "")))
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    interval = max(1.0, float(d.get("interval") or 5))
    W["stop"] = threading.Event()
    W["log"] = []
    W["thread"] = threading.Thread(
        target=watch_job,
        args=(match, (d.get("out") or "").strip(), interval,
              (d.get("cid") or "").strip(), (d.get("secret") or "").strip()),
        daemon=True)
    W["thread"].start()
    return {"ok": True}


def api_watch_stop(_):
    W["stop"].set()
    return {"ok": True}


def api_log(q):
    since = int(q.get("since", ["0"])[0])
    return {"lines": W["log"][since:], "next": len(W["log"]),
            "running": bool(W["thread"] and W["thread"].is_alive())}


# --- estadísticas y gráfico ----------------------------------------------------
def _paths(d):
    return [p.strip() for p in (d.get("paths") or "").splitlines() if p.strip()]


def api_stats(d):
    if (r := _falta("osuctools")):
        return r
    paths = _paths(d)
    if not paths:
        return {"error": "indicá al menos un archivo o carpeta .jsonl"}
    try:
        with _warnings_of() as warns:
            matches = load(*paths)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if not matches:
        return {"error": "no encontré partidas en esas rutas"}
    warns = _relevant(warns, {m.match_id for m in matches})
    top = int(d.get("top") or 0) or None
    skip = int(d.get("skip") or 0)
    if d.get("kind") == "torneo":
        summary = summarize(matches, skip_first=skip)
        return _with_warnings("\n".join(format_torneo(summary, top=top or 10)), warns)
    if d.get("kind") == "mvp":
        lines = []
        for m in matches:
            labels = team_labels(trim(m, skip))  # el país de cada equipo, si se puede saber
            lines += format_mvp(m, match_costs(m, skip_first=skip), top=top or 10,
                                labels=labels) + [""]
        return _with_warnings("\n".join(lines).rstrip(), warns)
    lines = format_header(matches)
    if d.get("kind") == "limpios":
        lines += format_clean(clean_players(matches, min_maps=int(d.get("min_maps") or 1)),
                              top=top or 10)
    else:
        lines += format_hardest(hardest_maps(matches, by=d.get("orden") or "auto"), top=top or 5)
    return _with_warnings("\n".join(lines), warns)


def api_files(_):
    found = [str(p) for p in Path.cwd().rglob("*.jsonl")
             if ".venv" not in p.parts][:200]
    return {"files": found, "cwd": str(Path.cwd())}


def _shortcuts():
    home = Path.home()
    cand = [("Inicio", home), ("Escritorio", home / "Desktop"), ("Escritorio", home / "Escritorio"),
            ("Descargas", home / "Downloads"), ("Descargas", home / "Descargas"),
            ("Documentos", home / "Documents"), ("Documentos", home / "Documentos"),
            ("Carpeta del programa", HERE), ("Carpeta de trabajo", Path.cwd())]
    if os.name == "nt":
        cand += [(f"Disco {c}:", Path(f"{c}:\\")) for c in "CDEFGHIJKLMNOPQRSTUVWXYZ"
                 if os.path.exists(f"{c}:\\")]
    else:
        cand.append(("Raíz /", Path("/")))
    out, seen = [], set()
    for name, p in cand:
        try:
            if p.is_dir() and str(p) not in seen:
                seen.add(str(p))
                out.append({"name": name, "path": str(p)})
        except OSError:
            pass
    return out


def api_browse(q):
    """Lista las carpetas y los archivos de una ruta, para el explorador de la interfaz."""
    raw = (q.get("path") or [""])[0].strip().strip('"')
    ext = ((q.get("ext") or [".jsonl"])[0] or ".jsonl").lower()
    p = Path(raw).expanduser() if raw else Path.cwd()
    try:
        while not p.is_dir() and p != p.parent:  # si no existe (o es un archivo), sube hasta una carpeta
            p = p.parent
        p = p.resolve()
        entries, count = [], 0
        for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if c.name.startswith("."):
                continue
            try:
                if c.is_dir():
                    entries.append({"name": c.name, "dir": True, "path": str(c)})
                elif c.suffix.lower() == ext:
                    count += 1
                    entries.append({"name": c.name, "dir": False, "path": str(c),
                                    "size": c.stat().st_size})
            except OSError:
                continue
    except PermissionError:
        return {"error": "sin permiso para entrar a esa carpeta"}
    except OSError as e:
        return {"error": str(e)}
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "sep": os.sep, "entries": entries,
            "count": count, "shortcuts": _shortcuts()}


def api_mkdir(d):
    name = (d.get("name") or "").strip()
    if not name or any(ch in name for ch in '/\\:*?"<>|') or name in (".", ".."):
        return {"error": "nombre de carpeta no válido"}
    target = Path(d.get("path") or ".") / name
    target.mkdir(parents=False, exist_ok=True)
    return {"path": str(target.resolve())}


def _dash_match(m0, skip):
    m = trim(m0, skip)
    labels, series, costs = team_labels(m), team_series(m), match_costs(m)
    last = series[-1] if series else None
    side, rows = {}, {}
    for g in m.games:
        for s in g.scores:
            if s.score <= 0:
                continue
            side[s.user_id] = s.team or side.get(s.user_id)
            if s.passed:
                r = rows.setdefault(s.user_id, [0, 0, 0.0, 0, 0])
                r[0] += 1; r[1] += s.score; r[2] += s.accuracy
                if not s.manual:
                    r[3] += s.statistics.get("count_miss", 0); r[4] += 1
    hard = hardest_maps([m])
    h = hard[0] if hard else None
    return {
        "id": m.match_id, "name": m.display_name, "closed": m.closed, "labels": labels,
        "sources": [str(x) for x in m0.sources],
        "rm": last.red_wins if last else 0, "bm": last.blue_wins if last else 0,
        "maps": [{"title": r.title, "version": r.version, "red": r.red, "blue": r.blue,
                  "winner": r.winner} for r in series],
        "costs": [{"uid": c.user_id, "name": c.username, "team": c.team, "cc": m.countries.get(c.user_id),
                   "maps": c.maps, "total": c.total_maps, "cost": c.match_cost, "ratio": c.avg_ratio}
                  for c in costs],
        "players": [{"uid": u, "name": m.username(u), "team": side.get(u), "cc": m.countries.get(u),
                     "maps": r[0], "score": r[1] / r[0], "acc": r[2] / r[0],
                     "miss": r[3] / r[4] if r[4] else None} for u, r in rows.items()],
        "hard": ({"title": h.title, "version": h.version, "stars": h.stars, "acc": h.avg_accuracy,
                  "miss": h.avg_misses} if h else None),
    }


def api_dashboard(d):
    if (r := _falta("osuctools")):
        return r
    paths = _paths(d)
    if not paths:
        return {"error": "indicá una carpeta o un archivo .jsonl"}
    skip = int(d.get("skip") or 0)
    try:
        with _warnings_of() as warns:
            matches = load(*paths)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    if not matches:
        return {"error": "no encontré partidas en esas rutas"}
    s = summarize(matches, skip_first=skip)
    torneo = {
        "n": s.matches, "games": s.games, "players": s.players, "beatmaps": s.beatmaps,
        "open": s.open_matches, "records": s.records, "skipped": s.skipped_standings,
        "standings": [{**asdict(t), "map_diff": t.map_diff} for t in s.standings],
        "results": [{**asdict(r), "winner": r.winner} for r in s.results],
        "top": [{"name": p.username, "cc": p.country, "matches": p.matches, "maps": p.maps,
                 "mvps": p.mvps, "cost": p.avg_cost, "ratio": p.avg_ratio, "score": p.avg_score,
                 "acc": p.avg_acc, "miss": p.misses_per_map if p.miss_n else None}
                for p in s.player_totals[:20]],
        "maps": [{"title": t.title, "version": t.version, "stars": t.stars, "plays": t.plays,
                  "score": t.avg_score, "acc": t.avg_acc, "miss": t.avg_misses if t.miss_n else None}
                 for t in s.map_totals[:12]],
    }
    return {"matches": [_dash_match(m, skip) for m in matches], "torneo": torneo,
            "warnings": _relevant(warns, {m.match_id for m in matches})}


ROUTES_POST = {"/api/watch/start": api_watch_start, "/api/watch/stop": api_watch_stop,
               "/api/stats": api_stats,
               "/api/mkdir": api_mkdir, "/api/dashboard": api_dashboard}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        if u.path == "/":
            try:
                page = PAGE_FILE.read_text(encoding="utf-8").replace("__MISSING__", json.dumps(MISSING))
            except OSError:
                return self._send(500, "Falta osu_panel.html: ponelo junto a osu_gui.py.", "text/plain")
            return self._send(200, page, "text/html")
        if u.path == "/api/log":
            return self._send(200, json.dumps(api_log(parse_qs(u.query))))
        if u.path == "/api/browse":
            return self._send(200, json.dumps(api_browse(parse_qs(u.query)), ensure_ascii=False))
        if u.path == "/api/files":
            return self._send(200, json.dumps(api_files(None)))
        self._send(404, "{}")

    def do_POST(self):  # noqa: N802
        fn = ROUTES_POST.get(urlparse(self.path).path)
        if not fn:
            return self._send(404, "{}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            res = fn(json.loads(self.rfile.read(n) or b"{}"))
        except Exception as e:  # noqa: BLE001
            res = {"error": str(e)}
        self._send(200, json.dumps(res, ensure_ascii=False))

    def log_message(self, *a):
        pass


PAGE_FILE = HERE / "osu_panel.html"


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"El puerto {PORT} está ocupado. Cerrá la otra instancia e intentá de nuevo.")
        return 1
    url = f"http://127.0.0.1:{PORT}"
    print(f"osu! Lobby Tools en {url}  (Ctrl+C para salir)")
    if MISSING:
        print("AVISO:", *MISSING, sep="\n  ")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        W["stop"].set()
        print("\nchau")
    return 0


if __name__ == "__main__":
    sys.exit(main())

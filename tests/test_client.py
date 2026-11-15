"""El cliente no debe provocar (ni agravar) un bloqueo de Cloudflare."""
import time

import pytest

from osu_cores.api import client as C

CF_HTML = "<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head>" + "x" * 5000


class Resp:
    def __init__(self, status=200, body=None, text="", ctype="application/json", headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text
        self.headers = {"content-type": ctype, **(headers or {})}

    def json(self):
        return self._body


TOKEN = Resp(200, {"access_token": "tok", "expires_in": 86400})


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(C, "THROTTLE", C._Throttle())
    monkeypatch.setattr(C, "MIN_GAP", 0.0)
    monkeypatch.setattr(C.time, "sleep", lambda s: None)
    calls = {"get": [], "post": []}
    monkeypatch.setattr(C.requests, "post", lambda url, **kw: calls["post"].append(kw) or TOKEN)
    return calls


def cli(cid="1"):
    return C.OsuClient(cid, "secret")


def test_manda_user_agent_propio(fresh, monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: fresh["get"].append(kw) or Resp(200, {"ok": 1}))
    cli().get_match(1)
    assert fresh["get"][0]["headers"]["User-Agent"] == C.USER_AGENT
    assert fresh["post"][0]["headers"]["User-Agent"] == C.USER_AGENT
    assert "python-requests" not in C.USER_AGENT


def test_token_compartido_entre_clientes(fresh, monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: Resp(200, {}))
    for _ in range(5):
        cli().get_match(1)  # 5 clientes distintos, mismas credenciales
    assert len(fresh["post"]) == 1


def test_403_de_cloudflare_no_se_reintenta_y_frena_todo(fresh, monkeypatch):
    n = []
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: n.append(1) or Resp(403, text=CF_HTML, ctype="text/html"))
    with pytest.raises(C.OsuBlockedError) as e:
        cli().get_match(1)
    assert len(n) == 1  # nada de reintentos
    assert e.value.retry_after >= 600
    assert len(str(e.value)) < 200  # y el mensaje no vuelca el HTML
    # otro cliente, otra partida: ni siquiera sale a la red mientras dure el bloqueo
    with pytest.raises(C.OsuBlockedError):
        cli().get_match(2)
    assert len(n) == 1


def test_bloqueos_seguidos_duplican_la_espera():
    t = C._Throttle()
    assert t.block() == C.BLOCK_COOLDOWN
    assert t.block() == C.BLOCK_COOLDOWN * 2
    for _ in range(10):
        t.block()
    assert t.block() == C.BLOCK_COOLDOWN_MAX


def test_un_request_exitoso_reinicia_la_escalada(fresh, monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: Resp(200, {}))
    C.THROTTLE.block()
    C.THROTTLE.ok()
    assert C.THROTTLE.block() == C.BLOCK_COOLDOWN


def test_403_json_de_la_api_no_se_toma_por_bloqueo(fresh, monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: Resp(403, {"error": "x"}, text='{"error":"x"}'))
    with pytest.raises(C.OsuApiError) as e:
        cli().get_match(1)
    assert not isinstance(e.value, C.OsuBlockedError)


def test_error_inesperado_se_recorta(fresh, monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: Resp(418, text="a" * 5000))
    with pytest.raises(C.OsuApiError) as e:
        cli().get_match(1)
    assert len(str(e.value)) < 300


def test_token_bloqueado_tambien_frena(fresh, monkeypatch):
    monkeypatch.setattr(C.requests, "post", lambda url, **kw: Resp(403, text=CF_HTML, ctype="text/html"))
    with pytest.raises(C.OsuBlockedError):
        cli().get_match(1)


def test_separa_los_requests_con_min_gap(monkeypatch):
    monkeypatch.undo()  # sin los parches del fixture: tiempo real
    t = C._Throttle()
    monkeypatch.setattr(C, "MIN_GAP", 0.05)
    t0 = time.monotonic()
    for _ in range(5):
        t.wait_turn()
    assert time.monotonic() - t0 >= 0.19  # 4 huecos de 0.05 s


# --- estado visible (lo que muestra la interfaz) ---------------------------------
def test_estado_unknown_blocked_unverified_ok():
    t = C._Throttle()
    assert t.status()["state"] == "unknown"
    t.block(60)
    st = t.status()
    assert st["state"] == "blocked" and 55 <= st["remaining"] <= 60
    t._blocked_until = time.time() - 1          # la pausa ya terminó, pero nadie comprobó nada
    assert t.status()["state"] == "unverified"
    t.ok()
    assert t.status()["state"] == "ok"


def test_el_bloqueo_sobrevive_a_un_reinicio(tmp_path):
    f = tmp_path / "estado.json"
    a = C._Throttle(f)
    a.block()                                   # "cierro el programa"
    b = C._Throttle(f)                          # "lo vuelvo a abrir"
    assert b.status()["state"] == "blocked"
    with pytest.raises(C.OsuBlockedError):
        b.check_blocked()
    b._blocked_until = time.time() - 1          # pasa la espera...
    b.ok()                                      # ...y la API vuelve a contestar bien
    assert C._Throttle(f)._strikes == 0         # la escalada quedó reiniciada también en disco


def test_archivo_de_estado_roto_no_rompe_nada(tmp_path):
    f = tmp_path / "estado.json"
    f.write_text("{no es json")
    assert C._Throttle(f).status()["state"] == "unknown"


def test_probe_no_sale_a_la_red_si_hay_pausa(fresh, monkeypatch):
    n = []
    monkeypatch.setattr(C.requests, "get", lambda url, **kw: n.append(1) or Resp(200, {}))
    C.THROTTLE.block(300)
    with pytest.raises(C.OsuBlockedError):
        cli().probe()
    assert n == []


def test_probe_distingue_bloqueo_de_sin_conexion(fresh, monkeypatch):
    def boom(url, **kw):
        raise C.requests.ConnectionError("sin internet")
    monkeypatch.setattr(C.requests, "get", boom)
    monkeypatch.setattr(C.requests, "post", lambda url, **kw: TOKEN)
    with pytest.raises(C.OsuNetworkError):
        cli().probe()
    assert C.THROTTLE.status()["state"] != "blocked"   # un corte de internet no es un bloqueo

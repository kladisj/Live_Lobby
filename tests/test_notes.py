"""Notas de equipos y jugadores (osu_gui.api_notes_*). No necesita la API de osu!."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import osu_gui  # noqa: E402


@pytest.fixture(autouse=True)
def notes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(osu_gui, "NOTES_FILE", tmp_path / "notas_equipos.json")
    return osu_gui.NOTES_FILE


def test_sin_archivo_no_hay_notas():
    assert osu_gui.api_notes_get() == {"notes": {}, "ok": True}


def test_guardar_leer_y_borrar_una_nota():
    assert osu_gui.api_notes_set({"key": "t:AR", "text": "Campeón 2022", "name": "Argentina"}) == {"ok": True}
    assert osu_gui.api_notes_set({"key": "p:123", "text": "Usa tableta", "name": "Facu"}) == {"ok": True}
    assert osu_gui.api_notes_get()["notes"] == {"t:AR": {"text": "Campeón 2022", "name": "Argentina"},
                                                "p:123": {"text": "Usa tableta", "name": "Facu"}}
    osu_gui.api_notes_set({"key": "t:AR", "text": "   "})  # vacía: se borra solo esa nota
    assert list(osu_gui.api_notes_get()["notes"]) == ["p:123"]


def test_ronda_manual_de_una_partida():
    assert osu_gui.api_notes_set({"key": "r:122095688", "text": "GROUPS"}) == {"ok": True}
    assert osu_gui.api_notes_get()["notes"]["r:122095688"]["text"] == "GROUPS"


@pytest.mark.parametrize("key", ["", "t:ar", "t:ARG", "p:abc", "x:1", "../p:1", "p:1234567890123"])
def test_claves_no_validas(key, notes_file):
    assert "error" in osu_gui.api_notes_set({"key": key, "text": "x"})
    assert not notes_file.exists()


def test_archivo_roto_se_respalda_y_no_se_pierde(notes_file):
    notes_file.write_text("{esto no es json", encoding="utf-8")
    assert osu_gui.api_notes_get()["ok"] is False
    osu_gui.api_notes_set({"key": "p:1", "text": "hola"})
    assert notes_file.with_name(notes_file.name + ".bak").read_text(encoding="utf-8") == "{esto no es json"
    assert json.loads(notes_file.read_text(encoding="utf-8"))["p:1"]["text"] == "hola"

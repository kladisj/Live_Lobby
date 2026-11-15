"""Parser de la hoja de mappools (osu_gui.parse_pools_csv). No necesita red."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import osu_gui  # noqa: E402

HEAD = ",,,banner,,id,artist - title [difficulty],sr,bpm,length,cs,ar,od,mapper,map id,featured artist,notes"

CSV = "\n".join([
    ",,,osu! World Cup 2026 ⬥ Mappools,,,,,,,,,,,,,,",
    ",,,,,,,,,,,,,,,,,",
    ",,,ROUND OF 16,,,,,,,,,,,,,,Download Mappack (163 MB)",
    HEAD,
    ",,,,,,,,,,,,,,,,,",
    ',,,,,NM1,BAD NERVES - YOU\'VE GOT THE NERVE [NO WAY],7.5★,220,02:23,3.9,9.8,9.3,steamyhotbun,5991670,,Aim',
    ",,,,,HD1,RADWIMPS - DADA [KING DEDEDELUXE COLLAB],7.1★,117,03:32,4,9.6,9,\"seros, FuJu\",5991808,,Fast Snap Aim",
    ",,,,,DT2,Waterflame - Radioactive [Meltdown],7.4★,222,01:47,4,10,10.1,netnesanya,5991788,fa listing,Stamina",
    ",,,,,TB,Pratanallis feat. KOKOMI - Reve d'ange [Resplendissant],7.7★,200,05:31,4,9.7,9.5,ponbot,5991904,,Reve d'ange",
    ",,,,,,,,,,,,,,,,,",
    ",,,GROUPS,,,,,,,,,,,,,,Download Mappack (101 MB)",
    HEAD,
    ",,,,,,,,,,,,,,,,,",
    ",,,,,NM1,Dirty Loops & Cory Wong - Thriller [owCEEHEE],7.2★,131,04:43,4,9.6,9,Mir,5963664,,Wide Angle Aim",
    ",,,,,FM2,SHIKI - Air [Another],6.4★,180,02:18,5,8,8,an3,https://osu.ppy.sh/b/5964023,,Antimod",
    ",,,,,,,,,,,,,,,,,",
])


def test_lee_un_bloque_por_ronda():
    r = osu_gui.parse_pools_csv(CSV)
    assert [x["name"] for x in r] == ["ROUND OF 16", "GROUPS"]
    assert [m["slot"] for m in r[0]["maps"]] == ["NM1", "HD1", "DT2", "TB"]
    assert [m["slot"] for m in r[1]["maps"]] == ["NM1", "FM2"]


def test_campos_de_un_mapa():
    nm1 = osu_gui.parse_pools_csv(CSV)[0]["maps"][0]
    assert nm1 == {"slot": "NM1", "title": "BAD NERVES - YOU'VE GOT THE NERVE [NO WAY]", "sr": 7.5, "bpm": 220.0,
                   "length": "02:23", "cs": 3.9, "ar": 9.8, "od": 9.3, "mapper": "steamyhotbun", "bid": 5991670,
                   "fa": "", "notes": "Aim"}


def test_textos_con_coma_y_links_de_mapa():
    r = osu_gui.parse_pools_csv(CSV)
    assert r[0]["maps"][1]["mapper"] == "seros, FuJu"
    assert r[0]["maps"][2]["fa"] == "fa listing"
    assert r[1]["maps"][1]["bid"] == 5964023  # el id puede venir como link


def test_sin_pools_devuelve_vacio():
    assert osu_gui.parse_pools_csv("a,b,c\n1,2,3\n") == []


def test_actualizar_con_csv_local_y_leer(tmp_path, monkeypatch):
    f = tmp_path / "pools.csv"
    f.write_text(CSV, encoding="utf-8")
    monkeypatch.setattr(osu_gui, "POOLS_FILE", tmp_path / "pools.json")
    monkeypatch.setattr(osu_gui, "POOLS_EXTRA", tmp_path / "pools_extra.json")
    (tmp_path / "pools_extra.json").write_text('{"5991670": {"tags": ["ORIGINAL", "NEW"]}}', encoding="utf-8")
    r = osu_gui.api_pools_update({"url": str(f)})
    assert [x["name"] for x in r["rounds"]] == ["ROUND OF 16", "GROUPS"] and r["url"] == str(f)
    assert r["rounds"][0]["maps"][0]["tags"] == ["ORIGINAL", "NEW"] and r["rounds"][0]["maps"][1]["tags"] == []
    assert osu_gui.api_pools_get()["rounds"][1]["maps"][0]["bid"] == 5963664


def test_errores_claros(tmp_path, monkeypatch):
    monkeypatch.setattr(osu_gui, "POOLS_FILE", tmp_path / "pools.json")
    assert "error" in osu_gui.api_pools_update({"url": ""})
    assert "error" in osu_gui.api_pools_update({"url": "no es un link"})
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert "error" in osu_gui.api_pools_update({"url": str(bad)})
    assert not (tmp_path / "pools.json").exists()


def test_bloques_copiados_se_descartan():
    """Plantillas de rondas futuras (mismos mapas que otra ronda) no deben aparecer como pools."""
    blk = lambda name: [  # noqa: E731
        f",,,{name},,,,,,,,,,,,,,", HEAD,
        ',,,,,NM1,A - A [a],7.5★,220,02:23,4,9.8,9.3,m,111,,x',
        ',,,,,HD1,B - B [b],7.0★,200,02:00,4,9,9,m,222,,y', ",,,,,,,,,,,,,,,,,"]
    csv_ = "\n".join(blk("GRAND FINALS") + blk("QUARTERFINALS") + blk("ROUND OF 16") +
                     [",,,GROUPS,,,,,,,,,,,,,,", HEAD, ',,,,,NM1,C - C [c],7.2★,131,04:43,4,9.6,9,m,333,,z'])
    assert [r["name"] for r in osu_gui.parse_pools_csv(csv_)] == ["ROUND OF 16", "GROUPS"]


def test_pools_json_viejo_con_duplicados_se_limpia_al_leer(tmp_path, monkeypatch):
    import json
    mp = lambda b: {"slot": "NM1", "title": "t", "bid": b}  # noqa: E731
    data = {"url": "u", "updated": "x", "rounds": [
        {"name": "FINALS", "maps": [mp(1)]}, {"name": "ROUND OF 16", "maps": [mp(1)]}, {"name": "GROUPS", "maps": [mp(2)]}]}
    monkeypatch.setattr(osu_gui, "POOLS_FILE", tmp_path / "pools.json")
    monkeypatch.setattr(osu_gui, "POOLS_EXTRA", tmp_path / "pe.json")
    (tmp_path / "pools.json").write_text(json.dumps(data), encoding="utf-8")
    assert [r["name"] for r in osu_gui.api_pools_get()["rounds"]] == ["ROUND OF 16", "GROUPS"]

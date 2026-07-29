"""emux_planes / emux_how — fleet discovery MCP tools.

The roster is DATA (the manager's product.json), never hardcoded; folklore
comes from per-plane `how` fields. A fresh agent must be able to learn the
fleet and how to drive each plane from these two tools alone.
"""

import asyncio
import json


def _roster(tmp_path, monkeypatch, data):
    p = tmp_path / "product.json"
    p.write_text(json.dumps(data))
    monkeypatch.setenv("DIRECTRUX_PRODUCT_JSON", str(p))
    return p


def test_emux_planes_reads_roster_live(tmp_path, monkeypatch):
    from emux import server

    _roster(
        tmp_path,
        monkeypatch,
        {
            "managed_planes": [
                {
                    "id": "gmux",
                    "lane": "greenmark",
                    "role": "worker",
                    "host": "rentamac",
                    "room": "https://example/gmux/room",
                }
            ],
            "fleet_extra_planes": [{"id": "emux", "lane": "eidos", "host": "laptop-01"}],
        },
    )
    r = asyncio.run(server.emux_planes())
    assert r["ok"] is True
    assert [p["id"] for p in r["planes"]] == ["gmux", "emux"]
    assert r["planes"][0]["room"] == "https://example/gmux/room"


def test_emux_planes_missing_roster_is_a_real_answer(tmp_path, monkeypatch):
    from emux import server

    monkeypatch.setenv("DIRECTRUX_PRODUCT_JSON", str(tmp_path / "nope.json"))
    r = asyncio.run(server.emux_planes())
    assert r["ok"] is False
    assert r["planes"] == []
    assert "could not read roster" in r["error"]


def test_emux_how_prefers_roster_folklore_verbatim(tmp_path, monkeypatch):
    from emux import server

    _roster(
        tmp_path,
        monkeypatch,
        {
            "managed_planes": [
                {"id": "gmux", "host": "rentamac", "how": "gmux — forwarded over SSH.\n  gmux ls"}
            ]
        },
    )
    r = asyncio.run(server.emux_how(plane="gmux"))
    assert r["ok"] is True
    assert r["text"].startswith("gmux — forwarded over SSH.")
    assert "Universal emux verbs" in r["text"]


def test_emux_how_unknown_plane_lists_known(tmp_path, monkeypatch):
    from emux import server

    _roster(tmp_path, monkeypatch, {"managed_planes": [{"id": "amux"}, {"id": "gmux"}]})
    r = asyncio.run(server.emux_how(plane="zzz"))
    assert r["ok"] is False
    assert r["known"] == ["amux", "gmux"]


def test_emux_how_generates_skeleton_and_all_planes(tmp_path, monkeypatch):
    from emux import server

    _roster(
        tmp_path,
        monkeypatch,
        {
            "managed_planes": [
                {"id": "reevux", "lane": "personal", "host": "mac-mini-01"},
                {
                    "id": "emux-e1",
                    "host": "epyc",
                    "controller": {"cli": "bin/plane", "endpoint": "https://ctl"},
                },
            ]
        },
    )
    r = asyncio.run(server.emux_how())
    assert r["ok"] is True
    assert "ssh mac-mini-01 '~/.local/bin/reevux <verb> ...'" in r["text"]
    assert "bin/plane capture|send emux-e1 <session>" in r["text"]

"""Managed plane probe: loopback preference + auth_gated (EID-1112/1115)."""
from __future__ import annotations

import json
from types import SimpleNamespace


def test_probe_prefers_loopback_healthy(monkeypatch):
    from emux import web
    from emux.product_config import ManagedPlane, ProductConfig

    calls: list[str] = []

    def fake_urlopen(req, timeout=1.0):  # noqa: ANN001
        url = req.full_url if hasattr(req, "full_url") else req.get_full_url()
        calls.append(url)

        class Resp:
            status = 200

            def read(self, n=65536):
                return json.dumps({"ok": True, "version": "t", "live_sessions": 3}).encode()

            def getcode(self):
                return 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        if "127.0.0.1" in url:
            return Resp()
        raise AssertionError("should not hit public when loopback healthy")

    import urllib.request

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: SimpleNamespace(open=fake_urlopen))

    cfg = ProductConfig(
        product="directrux",
        role="manager",
        chats_match="directrux",
        managed_planes=(
            ManagedPlane(
                id="amux",
                healthz="https://example.com/amux/healthz",
                healthz_loopback="http://127.0.0.1:8690/healthz",
            ),
        ),
        path="/tmp/test-product.json",
        source="file",
    )
    out = web._probe_managed_planes(cfg)
    assert out["workers_ok"] is True
    p = out["planes"][0]
    assert p["ok"] is True
    assert p["degraded"] is False
    assert p["reason"] == "healthy"
    assert p["probe_kind"] == "loopback"
    assert any("127.0.0.1" in u for u in calls)


def test_probe_auth_gated_public_is_ok_degraded(monkeypatch):
    import urllib.error
    import urllib.request

    from emux import web
    from emux.product_config import ManagedPlane, ProductConfig

    def fake_urlopen(req, timeout=1.0):  # noqa: ANN001
        raise urllib.error.HTTPError(
            req.full_url if hasattr(req, "full_url") else "https://x",
            303,
            "See Other",
            hdrs={},
            fp=None,
        )

    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: SimpleNamespace(open=fake_urlopen))

    cfg = ProductConfig(
        product="directrux",
        role="manager",
        chats_match="directrux",
        managed_planes=(ManagedPlane(id="gmux", healthz="https://example.com/gmux/healthz"),),
        path="/tmp/test-product.json",
        source="file",
    )
    out = web._probe_managed_planes(cfg)
    p = out["planes"][0]
    assert p["ok"] is True
    assert p["degraded"] is True
    assert p["reason"] == "auth_gated"
    assert out["workers_ok"] is True
    assert out["auth_gated"] == 1

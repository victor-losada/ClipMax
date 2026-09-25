import pytest

from clipmax.scheduler import SessionManager
from clipmax.timeutil import today_str
from clipmax.web.app import create_app


@pytest.fixture
def client(store, db):
    manager = SessionManager(store, db)
    app = create_app(store, db, manager)
    app.testing = True
    return app.test_client()


def test_pages_render(client, store):
    for url in ("/", "/config", "/config/yaml", "/sesiones", f"/sesion/{today_str(store.get())}", "/camara/westcol"):
        assert client.get(url).status_code == 200, url
    assert client.get("/sesion/1999-01-01").status_code == 404


def test_status_api(client):
    data = client.get("/api/estado").get_json()
    assert data["activa"] is False
    assert data["presupuesto"]["presupuesto"] == 10.0


def test_config_save_and_validation(client, store):
    cfg = store.get()
    cfg["evento"]["hora_inicio"] = "16:30"
    assert client.post("/api/config", json=cfg).get_json()["ok"] is True
    assert store.get()["evento"]["hora_inicio"] == "16:30"
    cfg["evento"]["hora_inicio"] = "no"
    r = client.post("/api/config", json=cfg)
    assert r.status_code == 400 and "hora_inicio" in r.get_json()["error"]


def test_x_context_and_camera(client, store, db):
    fecha = today_str(store.get())
    r = client.post(f"/api/sesion/{fecha}/x", json={"texto": "@a: Westcol vs Gear\n\n@b: Gear se picó con Westcol"})
    assert r.get_json()["posts"] == 2
    assert client.post("/api/sesion/hoy/x", json={"texto": "x"}).status_code == 400
    r = client.post("/api/camara/westcol", json={"x": 0.7, "y": 0.05, "w": 0.25, "h": 0.3})
    assert r.get_json()["ok"] is True
    assert store.get()["streamers"][0]["camara"]["x"] == 0.7


def test_import_requires_exported_candidates(client, store, db):
    fecha = today_str(store.get())
    db.get_or_create_session(fecha)
    r = client.post(f"/api/sesion/{fecha}/importar", json={"texto": "{}"})
    assert r.status_code == 400


def test_files_route_blocks_traversal(client, store):
    fecha = today_str(store.get())
    assert client.get(f"/archivos/{fecha}/../../../etc/passwd").status_code in (403, 404)


def test_publish_discarded_live_clip_after_the_session(client, db, monkeypatch):
    import threading

    from clipmax import liveclips

    s = db.get_or_create_session("2026-09-25")
    cid = db.add_live_clip(session_id=s["id"], slug="westcol", start_ts=100.0, end_ts=140.0, score=3.0,
                           estado="descartado", nota="banter")
    done = threading.Event()
    seen = []

    def fake_publish(self, clip_id):
        seen.append((self.session["id"], clip_id))
        done.set()
    monkeypatch.setattr(liveclips.LiveClipper, "publish", fake_publish)
    r = client.post(f"/api/clips-vivo/{cid}/publicar", json={})
    assert r.get_json()["ok"] is True
    assert done.wait(5) and seen == [(s["id"], cid)]
    assert db.live_clip(cid)["estado"] == "procesando"
    assert client.post("/api/clips-vivo/9999/publicar", json={}).status_code == 404


def test_session_steps_api(client, db):
    s = db.get_or_create_session("2026-09-25")
    db.set_pipeline_step(s["id"], "decidir", "en_curso", "Claude escribiendo… 1:20")
    r = client.get("/api/sesion/2026-09-25/pasos").get_json()
    assert r["pasos"]["decidir"]["detalle"].startswith("Claude escribiendo") and r["corriendo"] is False
    assert client.get("/api/sesion/1999-01-01/pasos").status_code == 404

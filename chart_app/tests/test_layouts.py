"""Named layout REST API: file storage, sanitize, last-used."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import pytest

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(APP))

os.environ.setdefault("CHART_APP_WS_MOCK", "1")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHART_APP_LAYOUTS", str(tmp_path))
    monkeypatch.setenv("CHART_APP_WS_MOCK", "1")
    from fastapi.testclient import TestClient

    import server as srv

    with TestClient(srv.app) as c:
        yield c, tmp_path


def test_put_get_same_json(client):
    c, root = client
    body = {"version": 1, "name": "T1", "state": {"chart_app.indicators.BTCUSD": "[]", "chartTf": "15m"}}
    r = c.put("/api/layouts/T1", json=body)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["version"] == 1
    assert got["name"] == "T1"
    assert got["state"] == body["state"]
    assert "saved_at" in got
    r2 = c.get("/api/layouts/T1")
    assert r2.status_code == 200
    again = r2.json()
    assert again["state"] == body["state"]
    assert again["name"] == "T1"
    assert (root / "T1.json").is_file()


def test_list_and_delete(client):
    c, root = client
    c.put("/api/layouts/A", json={"state": {"k": 1}})
    c.put("/api/layouts/B", json={"state": {"k": 2}})
    rows = c.get("/api/layouts").json()
    names = sorted(x["name"] for x in rows)
    assert names == ["A", "B"]
    assert all("updated_at" in x for x in rows)
    d = c.delete("/api/layouts/A")
    assert d.status_code == 200
    assert not (root / "A.json").is_file()
    names2 = sorted(x["name"] for x in c.get("/api/layouts").json())
    assert names2 == ["B"]


def test_reject_path_traversal(client):
    c, _root = client
    r = c.put("/api/layouts/../x", json={"state": {}})
    assert r.status_code >= 400
    r2 = c.put("/api/layouts/" + quote("../x", safe=""), json={"state": {}})
    assert r2.status_code >= 400
    r3 = c.put("/api/layouts/bad.name", json={"state": {}})
    assert r3.status_code == 400
    listed = c.get("/api/layouts").json()
    assert listed == []


def test_last_set_get(client):
    c, root = client
    c.put("/api/layouts/T1", json={"state": {"chartTf": "1m"}})
    r = c.put("/api/layouts/_last", json={"name": "T1"})
    assert r.status_code == 200
    assert r.json()["name"] == "T1"
    g = c.get("/api/layouts/_last")
    assert g.json()["name"] == "T1"
    assert (root / "_last.json").is_file()


def test_file_created_in_layouts_dir(client):
    c, root = client
    c.put("/api/layouts/My Layout", json={"state": {"chartTf": "5m"}})
    path = root / "My Layout.json"
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "My Layout"
    assert data["state"]["chartTf"] == "5m"

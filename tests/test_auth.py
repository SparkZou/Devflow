"""面板登录：Cookie 会话、HTTP Basic、开放路径、防暴力。"""
from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from devflow.auth import COOKIE, Sessions
from devflow.server import create_app
from test_smoke import make


@pytest.fixture
def client(tmp_path):
    cfg, store, p = make(tmp_path)
    app = create_app(cfg, store, p)
    with TestClient(app, follow_redirects=False) as c:
        c.cfg = cfg
        yield c


def basic(user="admin", pw="admin2026"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_health_is_open(client):
    assert client.get("/health").status_code == 200


def test_pages_redirect_to_login(client):
    r = client.get("/task/1?x=1")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2Ftask%2F1%3Fx%3D1"
    assert client.get("/login").status_code == 200
    assert client.get("/api/tasks").status_code == 401


def test_login_logout_flow(client):
    r = client.post("/login", data={"username": "admin", "password": "wrong", "next": "/"})
    assert r.status_code == 401 and "用户名或密码不对" in r.text
    r = client.post("/login", data={"username": "admin", "password": "admin2026", "next": "/task/9"})
    assert r.status_code == 303 and r.headers["location"] == "/task/9" and COOKIE in r.cookies
    assert client.get("/").status_code == 200
    assert client.get("/api/tasks").status_code == 200
    assert client.get("/login").status_code == 303  # 已登录再开登录页 → 直接回面板
    r = client.get("/logout")
    assert r.status_code == 303 and client.get("/").status_code == 303


def test_open_redirect_blocked(client):
    r = client.post("/login", data={"username": "admin", "password": "admin2026", "next": "//evil.com/x"})
    assert r.headers["location"] == "/"


def test_api_basic_auth(client):
    assert client.get("/api/tasks", headers=basic()).status_code == 200
    assert client.get("/api/tasks", headers=basic(pw="nope")).status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": "Basic not-base64"}).status_code == 401


def test_lockout_after_repeated_failures(client):
    for _ in range(5):
        client.post("/login", data={"username": "admin", "password": "x"})
    r = client.post("/login", data={"username": "admin", "password": "admin2026"})
    assert r.status_code == 429 and "错误次数太多" in r.text


def test_auth_can_be_disabled(tmp_path):
    cfg, store, p = make(tmp_path)
    cfg.auth.enabled = False
    with TestClient(create_app(cfg, store, p), follow_redirects=False) as c:
        assert c.get("/").status_code == 200 and c.get("/api/tasks").status_code == 200
        assert c.get("/login").status_code == 303


def test_session_token_invalidated_by_password_change(tmp_path):
    cfg, _, _ = make(tmp_path)
    s = Sessions(cfg)
    tok = s.issue()
    assert s.valid(tok) and not s.valid("garbage") and not s.valid("1.abc")
    assert Sessions(cfg).valid(tok)  # 密钥落在 data/.session_secret，重启后会话仍有效
    cfg.auth.password = "changed"
    assert not Sessions(cfg).valid(tok)

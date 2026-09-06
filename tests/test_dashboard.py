"""导航面板：左栏项目分类/计数、按项目过滤、线上监控与证书到期。"""
from __future__ import annotations

import base64
import datetime as dt

from fastapi.testclient import TestClient

from devflow.models import Task
from devflow.monitor import SiteMonitor, parse_not_after
from devflow.server import create_app
from test_smoke import make

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin2026").decode()}


def fake_result(url, days=10, ok=True):
    return {"url": url, "host": url.split("//")[1], "checked_at": dt.datetime(2026, 9, 6, 10, 0),
            "ok": ok, "status": 200 if ok else 502, "ms": 123, "error": "" if ok else "boom",
            "cert_expires": dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc), "cert_days": days,
            "cert_issuer": "Let's Encrypt", "cert_error": ""}


def setup(tmp_path):
    cfg, store, p = make(tmp_path)
    cfg.projects[0].deploy_url = "https://demo.example.com"
    cfg.projects[1].deploy_url = "https://api.example.com"
    cfg.projects[1].kind = "own"
    store.save(Task(raw_text="a", title="AAA-task", project="demo", state="issue"))
    store.save(Task(raw_text="b", title="BBB-task", project="demo-api", state="coding"))
    store.save(Task(raw_text="c", title="CCC-task", state="todo"))  # 未归类，等你选项目
    mon = SiteMonitor(cfg)
    mon._results = {"https://demo.example.com": fake_result("https://demo.example.com", days=10),
                    "https://api.example.com": fake_result("https://api.example.com", days=80, ok=False)}
    mon._refreshed_at = dt.datetime(2026, 9, 6, 10, 0)
    return cfg, store, p, mon


def test_sidebar_counts_and_filters(tmp_path):
    cfg, store, p, mon = setup(tmp_path)
    with TestClient(create_app(cfg, store, p, mon), follow_redirects=False) as c:
        html = c.get("/", headers=AUTH).text
        assert "AAA-task" in html and "BBB-task" in html and "CCC-task" in html
        assert "客户项目" in html and "自己的项目" in html and "未归类" in html
        assert 'href="/?project=demo"' in html and 'href="/?project=group%3Ademo"' in html
        assert "🔒10天" in html  # demo 证书 10 天后到期 → 左栏告警

        html = c.get("/?project=demo", headers=AUTH).text
        assert "AAA-task" in html and "BBB-task" not in html and "CCC-task" not in html
        assert "demo.example.com" in html and "api.example.com" not in html
        assert 'value="demo" selected' in html  # 收件框预选当前项目
        assert 'name="back" value="/?project=demo"' in html

        html = c.get("/?project=group%3Ademo", headers=AUTH).text
        assert "BBB-task" in html and "AAA-task" not in html and "demo › api" in html
        html = c.get("/?project=kind%3Aown", headers=AUTH).text
        assert "BBB-task" in html and "AAA-task" not in html
        html = c.get("/?project=none", headers=AUTH).text
        assert "CCC-task" in html and "AAA-task" not in html


def test_monitor_table_and_refresh(tmp_path, monkeypatch):
    cfg, store, p, mon = setup(tmp_path)
    with TestClient(create_app(cfg, store, p, mon), follow_redirects=False) as c:
        html = c.get("/", headers=AUTH).text
        assert "2026-09-16" in html and "（剩 10 天）" in html and "cert-bad" in html
        assert "（剩 80 天）" in html and "cert-ok" in html
        assert "✅ 200" in html and "❌ 502" in html and "Let&#39;s Encrypt" in html
        assert "最近到期：demo.example.com" in html

        calls = []
        monkeypatch.setattr("devflow.monitor.check_site",
                            lambda url, health="": calls.append(url) or fake_result(url, days=3))
        r = c.post("/monitor/refresh", data={"back": "/?project=demo"}, headers=AUTH)
        assert r.status_code == 303 and r.headers["location"] == "/?project=demo"
        assert sorted(calls) == ["https://api.example.com", "https://demo.example.com"]
        assert "剩 3 天" in c.get("/?project=demo", headers=AUTH).text


def test_monitor_helpers(tmp_path):
    assert parse_not_after("Nov 13 09:12:00 2026 GMT") == dt.datetime(2026, 11, 13, 9, 12, tzinfo=dt.timezone.utc)
    cfg, store, p, mon = setup(tmp_path)
    exp = mon.expiring(days=14)
    assert [x[0].name for x in exp] == ["demo"] and exp[0][1]["cert_days"] == 10
    assert mon.for_project(cfg.projects[1])["ok"] is False
    cfg.projects[0].deploy_url = ""
    assert mon.for_project(cfg.projects[0]) is None and len(mon.rows()) == 1


def test_digest_mentions_expiring_cert(tmp_path):
    from devflow.scheduler import build_digest

    cfg, store, p, mon = setup(tmp_path)
    _, body = build_digest(store, cfg, mon)
    assert "证书" in body and "demo.example.com" in body and "10 天" in body
    _, body = build_digest(store, cfg)
    assert "证书" not in body

"""自动合并（需要 CI 通过）+ 和 GitHub 对账（本地 Closes #N / 网页合并 PR）。"""
from __future__ import annotations

import base64

from fastapi.testclient import TestClient

from devflow.config import GatesCfg
from devflow.models import Task
from devflow.server import create_app
from test_smoke import make

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin2026").decode()}


def pr(checks="success", state="OPEN", merged=False, failed=None):
    return {"state": state, "merged": merged, "merged_at": "", "merge_sha": "abc1234" if merged else "",
            "head_sha": "h", "head_ref": "b", "checks": checks, "failed": failed or [], "url": "https://x/pull/9"}


def open_pr_task(store, **kw):
    fields = dict(raw_text="x", title="加导出按钮", project="demo", state="pr_open", pr_number=9, issue_number=5,
                  pr_url="https://x/pull/9", branch="devflow/1-x")
    fields.update(kw)
    t = Task(**fields)
    store.save(t)
    return t


def test_auto_merge_waits_when_repo_has_no_ci(tmp_path, monkeypatch):
    merged = []
    monkeypatch.setattr("devflow.pipeline.github.merge_pr", lambda repo, n: merged.append(n) or "abc1234")
    cfg, store, p = make(tmp_path, gates=GatesCfg(merge=False))
    t = open_pr_task(store)

    monkeypatch.setattr("devflow.pipeline.github.pr_status", lambda repo, n: pr(checks="none"))
    p._check_ci(store.get(t.id))
    t = store.get(t.id)
    assert t.state == "ready_to_merge" and "本地 build" in t.events[-1].msg and not merged
    assert any("没有 CI" in title for title in p.notifier.sent)

    # 仓库配了 CI 且通过 → 自动合并 → 部署中
    monkeypatch.setattr("devflow.pipeline.github.pr_status", lambda repo, n: pr(checks="success"))
    t.set_state("pr_open", "again")
    store.save(t)
    p._check_ci(store.get(t.id))
    assert merged == [9] and store.get(t.id).state == "deploying"

    # merge_requires_ci=false 时，没 CI 也自动合并
    cfg.pipeline.merge_requires_ci = False
    t2 = open_pr_task(store)
    monkeypatch.setattr("devflow.pipeline.github.pr_status", lambda repo, n: pr(checks="none"))
    p._check_ci(store.get(t2.id))
    assert merged == [9, 9] and store.get(t2.id).state == "deploying"


def test_project_auto_merge_overrides_gate(tmp_path, monkeypatch):
    merged = []
    monkeypatch.setattr("devflow.pipeline.github.merge_pr", lambda repo, n: merged.append(n) or "abc1234")
    monkeypatch.setattr("devflow.pipeline.github.pr_status", lambda repo, n: pr(checks="success"))
    cfg, store, p = make(tmp_path, gates=GatesCfg(merge=True))
    t = open_pr_task(store)
    p._check_ci(store.get(t.id))
    assert store.get(t.id).state == "ready_to_merge" and not merged  # 全局要确认

    cfg.projects[0].auto_merge = True  # 这个仓库单独放开
    t.set_state("pr_open", "again")
    store.save(t)
    p._check_ci(store.get(t.id))
    assert merged == [9] and store.get(t.id).state == "deploying"

    cfg.gates.merge, cfg.projects[0].auto_merge = False, False  # 全局自动，这个仓库单独要确认
    t3 = open_pr_task(store)
    p._check_ci(store.get(t3.id))
    assert merged == [9] and store.get(t3.id).state == "ready_to_merge"


def test_sync_github_closes_orphans_and_follows_manual_merge(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("devflow.pipeline.github.close_pr", lambda repo, n, c="": calls.append(("close_pr", n)))
    monkeypatch.setattr("devflow.pipeline.github.delete_remote_branch", lambda p, b: calls.append(("branch", b)) or True)
    monkeypatch.setattr("devflow.pipeline.github.delete_local_branch", lambda p, b: None)
    monkeypatch.setattr("devflow.pipeline.github.close_issue", lambda *a, **k: calls.append(("close_issue", a[1])))
    cfg, store, p = make(tmp_path)

    # #1：PR 还开着，但你本地 commit 写了 Closes #5 → Issue 已关 → 收口
    a = open_pr_task(store)
    a.set_state("ready_to_merge", "CI 通过")
    store.save(a)
    # #2：你在网页上合并了 PR #10 → 进入部署
    b = open_pr_task(store, pr_number=10, issue_number=6)
    b.set_state("ci_failed", "x")
    store.save(b)
    # #3：还没开 PR、Issue 也没关 → 不动
    c = Task(raw_text="y", title="t", project="demo", state="issue", issue_number=7)
    store.save(c)

    prs = {9: pr(), 10: pr(state="MERGED", merged=True)}
    issues = {5: "CLOSED", 6: "OPEN", 7: "OPEN"}
    monkeypatch.setattr("devflow.pipeline.github.pr_status", lambda repo, n: prs[n])
    monkeypatch.setattr("devflow.pipeline.github.issue_state",
                        lambda repo, n: {"state": issues[n], "closed_at": "", "reason": ""})
    changed = p.sync_github()

    a, b, c = store.get(a.id), store.get(b.id), store.get(c.id)
    assert a.state == "delivered" and "自动标记已解决" in a.events[-1].msg
    assert ("close_pr", 9) in calls and ("branch", "devflow/1-x") in calls and ("close_issue", 5) not in calls  # Issue 已关，不再关一次
    assert b.state == "deploying" and b.merge_sha == "abc1234"
    assert c.state == "issue"
    assert len(changed) == 2 and any("已解决" in x for x in changed) and any("部署中" in x for x in changed)
    assert any("同步" in title for title in p.notifier.sent)

    # 再同步一次：都已收口，什么都不做
    assert p.sync_github() == []


def test_sync_endpoints(tmp_path, monkeypatch):
    cfg, store, p = make(tmp_path)
    monkeypatch.setattr(p, "sync_github", lambda: ["#1 ok"])
    with TestClient(create_app(cfg, store, p), follow_redirects=False) as c:
        r = c.post("/sync", data={"back": "/?project=demo"}, headers=AUTH)
        assert r.status_code == 303 and r.headers["location"] == "/?project=demo"
        assert c.post("/api/sync", headers=AUTH).json() == {"ok": True, "changed": ["#1 ok"]}
        assert "同步 GitHub" in c.get("/", headers=AUTH).text

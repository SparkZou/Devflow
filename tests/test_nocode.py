"""不需要改代码的命令（取文件 / 问问题）也要处理完：不留「出错」任务和孤儿 Issue。"""
from __future__ import annotations

import base64
import datetime as dt

from fastapi.testclient import TestClient

from devflow.integrations.claude_code import CoderResult
from devflow.models import Task
from devflow.scheduler import build_digest
from devflow.server import create_app
from test_smoke import make

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:admin2026").decode()}
REPORT = "**结论**：这张图不在仓库里。GTM 宣传图由本地脚本生成到 docs/articles/gtm-studio/（被 .gitignore 忽略），需要在开发机上取。"


def patch_git(monkeypatch, closed):
    monkeypatch.setattr("devflow.pipeline.github.prepare_worktree", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.delete_local_branch", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.has_changes", lambda wt: False)
    monkeypatch.setattr("devflow.pipeline.github.commit_log", lambda wt, base: "")
    monkeypatch.setattr("devflow.pipeline.github.create_pr", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该开 PR")))
    monkeypatch.setattr("devflow.pipeline.github.close_issue", lambda repo, n, c="": closed.append((n, c)))
    monkeypatch.setattr("devflow.pipeline.Pipeline._push_draft_to_clipboard", lambda self, t: None)


def issue_task(store):
    t = Task(raw_text="从这个项目下帮我取个图片，昨天生成的 GTM 功能图，取了给我", title="取昨日GTM功能介绍图片",
             project="demo", state="issue", issue_number=13, source="wechat")
    store.save(t)
    return t


def test_no_diff_becomes_conclusion_and_closes_issue(tmp_path, monkeypatch):
    closed = []
    patch_git(monkeypatch, closed)
    monkeypatch.setattr("devflow.pipeline.run_claude",
                        lambda prompt, **k: CoderResult(ok=True, result=REPORT, num_turns=6, cost_usd=0.2))
    cfg, store, p = make(tmp_path)
    t = issue_task(store)
    p.run("code", t.id)
    t = store.get(t.id)
    assert t.state == "ready_to_deliver" and t.error == "" and t.is_investigation  # 没有卡在「出错」
    assert "不在仓库里" in t.report and t.coder_summary == REPORT and t.reply_draft
    assert closed and closed[0][0] == 13 and "不在仓库里" in closed[0][1]  # Issue 用结论收口
    assert t.pr_number is None and t.branch == ""


def test_no_diff_without_summary_reruns_as_investigation(tmp_path, monkeypatch):
    closed, prompts = [], []

    def fake(prompt, **k):
        prompts.append(prompt)
        return CoderResult(ok=True, result="" if len(prompts) == 1 else REPORT, num_turns=3, cost_usd=0.1)

    patch_git(monkeypatch, closed)
    monkeypatch.setattr("devflow.pipeline.run_claude", fake)
    cfg, store, p = make(tmp_path)
    t = issue_task(store)
    p.run("code", t.id)
    t = store.get(t.id)
    assert len(prompts) == 2 and "只做检查、不改代码" in prompts[1] and "github.com/o/r/blob/main" in prompts[1]
    assert t.state == "ready_to_deliver" and "不在仓库里" in t.report
    assert [n for n, _ in closed] == [13]


def test_coder_summary_survives_a_later_failure(tmp_path, monkeypatch):
    patch_git(monkeypatch, [])
    monkeypatch.setattr("devflow.pipeline.github.has_changes", lambda wt: True)
    monkeypatch.setattr("devflow.pipeline.github.commit_and_push",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("push 被拒")))
    monkeypatch.setattr("devflow.pipeline.run_claude",
                        lambda prompt, **k: CoderResult(ok=True, result="改了导出按钮", num_turns=5, cost_usd=0.3))
    cfg, store, p = make(tmp_path)
    t = issue_task(store)
    p.run("code", t.id)
    t = store.get(t.id)
    assert t.state == "failed" and "push 被拒" in t.error and t.coder_summary == "改了导出按钮"


def test_waiting_age_on_cards_and_digest(tmp_path):
    cfg, store, p = make(tmp_path)
    t = Task(raw_text="x", title="卡住的任务", project="demo", error="boom")
    t.set_state("failed", "❌ code 失败")
    t.events[-1].at = (dt.datetime.now() - dt.timedelta(days=2, hours=3)).replace(microsecond=0).isoformat(sep=" ")
    store.save(t)  # 保存会刷新 updated_at，但等待时长按最后一条事件算
    t = store.get(t.id)
    assert 50 < t.waiting_hours < 52 and t.waiting_label == "2 天"
    fresh = Task(raw_text="y", title="刚失败", project="demo")
    fresh.set_state("failed", "❌")
    store.save(fresh)
    assert store.get(fresh.id).waiting_label == "0 小时"

    _, body = build_digest(store, cfg)
    assert "卡了超过 1 天 1 个" in body and "⏳已等 2 天" in body
    with TestClient(create_app(cfg, store, p), follow_redirects=False) as c:
        html = c.get("/", headers=AUTH).text
        assert html.count("⏳ 已等") == 1 and "已等 2 天" in html

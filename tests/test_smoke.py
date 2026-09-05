"""不联网的冒烟测试：状态机、存储、JSON 解析、CI 汇总。"""
from __future__ import annotations

import pytest

from devflow.ai import TestVerdict, TriageResult, extract_json
from devflow.config import Config, GatesCfg, ProjectCfg
from devflow.integrations.github import _summarize_checks
from devflow.models import Task, needs_action
from devflow.pipeline import Pipeline
from devflow.scheduler import build_digest
from devflow.store import Store


class FakeAI:
    def __init__(self, is_req=True, project="demo", passed=True, extra=None, task_type="implement"):
        self.is_req, self.project, self.passed, self.extra = is_req, project, passed, extra or []
        self.task_type = task_type
        self.backend = type("B", (), {"name": "fake"})()
        self.last_hint, self.last_images = None, None

    def investigation_reply(self, *a, **k):
        return "查过了，已经解决，请验证。"

    def triage(self, raw, source, images=None, group_hint=""):
        self.last_hint, self.last_images = group_hint, images or []
        return TriageResult(
            is_requirement=self.is_req, task_type=self.task_type, confidence=0.9, reason="测试", title="加导出按钮",
            description="在订单列表加导出 Excel 按钮", acceptance_criteria=["有按钮", "能下载"],
            priority="P1", customer="张总", project=self.project, extra_projects=self.extra,
            deadline="2026-09-01", questions=[],
        )

    def judge_test(self, *a, **k):
        return TestVerdict(passed=self.passed, confidence=0.8, summary="看起来正常", issues=[])

    def release_note(self, *a, **k):
        return "## 本次更新\n加了导出"

    def customer_reply(self, *a, **k):
        return "张总，导出功能上线了，请验收。"


class NullNotifier:
    def __init__(self):
        self.sent = []

    def me(self, title, body, desktop=True):
        self.sent.append(title)


def make(tmp_path, gates=None, ai=None):
    cfg = Config(
        projects=[
            ProjectCfg(name="demo", github_repo="o/r", repo_path=str(tmp_path), deploy_url=""),
            ProjectCfg(name="demo-api", group="demo", github_repo="o/r-api", repo_path=str(tmp_path)),
        ],
        gates=gates or GatesCfg(), data_dir=str(tmp_path / "data"),
    )
    cfg._path = tmp_path / "config.yaml"
    store = Store(cfg.db_path)
    p = Pipeline(cfg, store, ai=ai or FakeAI(), notifier=NullNotifier(), sync=True)
    return cfg, store, p


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "x.sqlite3")
    t = Task(raw_text="hello", source="wechat")
    s.save(t)
    assert t.id == 1
    t.set_state("todo", "x")
    s.save(t)
    got = s.get(1)
    assert got.state == "todo" and got.events[-1].msg == "x"
    assert [x.id for x in s.list(["todo"])] == [1]
    s.kv_set("k", "v")
    assert s.kv_get("k") == "v"


def test_fill_defaults_tolerates_missing_fields():
    from devflow.ai import fill_defaults

    data = fill_defaults(TriageResult, {"is_requirement": False, "confidence": 0.9, "reason": "寒暄"})
    r = TriageResult.model_validate(data)
    assert r.title == "" and r.acceptance_criteria == [] and r.priority == "P0" and r.task_type == "implement"


def test_resume_reruns_confirmed_merge(tmp_path, monkeypatch):
    monkeypatch.setattr("devflow.pipeline.github.merge_pr", lambda *a, **k: "abc1234")
    cfg, store, p = make(tmp_path)
    t = Task(raw_text="x", title="t", project="demo", state="ready_to_merge", pr_number=1)
    t.log("👤 确认：merge")
    store.save(t)
    assert p.resume() == 1
    assert store.get(t.id).state == "deploying"


def test_parse_claude_output_errors_are_readable():
    import json

    from devflow.integrations.claude_code import parse_output

    out = json.dumps({
        "is_error": True, "subtype": "error_max_turns", "result": "", "session_id": "abc", "num_turns": 80,
        "total_cost_usd": 7.71,
        "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": 'cd "C:/x y" && npm test'}}] * 3,
    })
    r = parse_output(out, 1)
    assert not r.ok and r.subtype == "error_max_turns" and r.session_id == "abc" and len(r.denials) == 3
    assert "达到最大轮数" in r.error_summary and "3 次命令因权限被拒" in r.error_summary and "npm test" in r.error_summary
    ok = parse_output(json.dumps({"is_error": False, "result": "改好了", "num_turns": 5}), 0)
    assert ok.ok and ok.result == "改好了" and ok.denials == []


def test_coder_resumes_once_on_max_turns(tmp_path, monkeypatch):
    from devflow.integrations.claude_code import CoderResult

    calls = []

    def fake_run(prompt, **kw):
        calls.append(kw.get("resume", ""))
        if not kw.get("resume"):
            return CoderResult(ok=False, subtype="error_max_turns", session_id="s1", num_turns=120, cost_usd=1.0)
        return CoderResult(ok=True, result="收尾完成", num_turns=10, cost_usd=0.5)

    monkeypatch.setattr("devflow.pipeline.run_claude", fake_run)
    cfg, store, p = make(tmp_path)
    res = p._run_coder(tmp_path, "做点什么")
    assert calls == ["", "s1"] and res.ok and res.num_turns == 130 and res.cost_usd == 1.5


def test_extract_json():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('前面有话\n```json\n{"a": 2}\n```\n后面') == {"a": 2}
    assert extract_json('blah {"a": {"b": 3}} tail') == {"a": {"b": 3}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_summarize_checks():
    assert _summarize_checks(None) == ("none", [])
    assert _summarize_checks([{"__typename": "CheckRun", "status": "IN_PROGRESS"}]) == ("pending", [])
    assert _summarize_checks([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"},
                              {"__typename": "StatusContext", "state": "SUCCESS"}]) == ("success", [])
    assert _summarize_checks([{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE", "name": "test"},
                              {"__typename": "CheckRun", "status": "IN_PROGRESS"}]) == ("failure", ["test"])


def test_triage_not_requirement(tmp_path):
    cfg, store, p = make(tmp_path, ai=FakeAI(is_req=False))
    t = p.ingest("谢谢，辛苦了", source="wechat")
    assert t.state == "not_task"
    assert not needs_action(t, cfg.gates)


def test_triage_without_project_waits(tmp_path):
    cfg, store, p = make(tmp_path, ai=FakeAI(project=""))
    t = p.ingest("张总：订单列表加个导出 Excel 按钮", source="wechat")
    assert t.state == "todo" and t.project == "" and t.priority == "P1"
    assert needs_action(t, cfg.gates)
    with pytest.raises(ValueError):
        p.approve(t.id)  # 没选项目不能建 Issue
    p.update(t.id, project="demo")
    assert store.get(t.id).project == "demo"


def test_triage_gate_stops_before_issue(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr("devflow.pipeline.github.create_issue", lambda *a, **k: called.append(1) or (7, "https://x/issues/7"))
    cfg, store, p = make(tmp_path, gates=GatesCfg(triage=True, code=True))
    t = p.ingest("加导出按钮", source="dingtalk")
    assert t.state == "todo" and not called
    assert needs_action(t, cfg.gates)
    assert p.approve(t.id) == "create_issue"
    t = store.get(t.id)
    assert t.state == "issue" and t.issue_number == 7 and called


def test_auto_issue_then_code_gate(tmp_path, monkeypatch):
    monkeypatch.setattr("devflow.pipeline.github.create_issue", lambda *a, **k: (8, "https://x/issues/8"))
    cfg, store, p = make(tmp_path, gates=GatesCfg(code=True))
    t = p.ingest("加导出按钮", source="wechat")
    assert t.state == "issue" and t.issue_url.endswith("/8")
    assert needs_action(t, cfg.gates)


def test_failed_step_is_recorded_and_retryable(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("gh 未登录")

    monkeypatch.setattr("devflow.pipeline.github.create_issue", boom)
    cfg, store, p = make(tmp_path)
    t = p.ingest("加导出按钮", source="wechat")
    assert t.state == "failed" and "gh 未登录" in t.error and t.prev_state == "todo"
    monkeypatch.setattr("devflow.pipeline.github.create_issue", lambda *a, **k: (9, "https://x/issues/9"))
    monkeypatch.setattr(cfg.gates, "code", True)
    assert p.retry(t.id) == "create_issue"
    assert store.get(t.id).state == "issue"


def test_delivery_draft_and_deliver(tmp_path, monkeypatch):
    monkeypatch.setattr("devflow.pipeline.github.pr_summary", lambda *a, **k: ("- c1", "- f1"))
    monkeypatch.setattr("devflow.pipeline.github.comment_issue", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.close_issue", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.Pipeline._push_draft_to_clipboard", lambda self, t: None)
    monkeypatch.setattr("devflow.pipeline.browser_test.run_browser_check",
                        lambda url, shot: {"ok": True, "url": url, "status": 200, "title": "t", "console_errors": []})
    cfg, store, p = make(tmp_path)
    cfg.projects[0].deploy_url = "https://example.com"
    t = Task(raw_text="x", title="加导出按钮", project="demo", state="deployed", source="wechat", pr_number=1, issue_number=1)
    store.save(t)
    assert p.approve(t.id) == "test"
    t = store.get(t.id)
    assert t.state == "ready_to_deliver" and t.test_passed is True and t.reply_draft
    assert p.approve(t.id) == "deliver"
    assert store.get(t.id).state == "delivered"

    # 没配 deploy_url：跳过测试，直接出草稿
    cfg.projects[0].deploy_url = ""
    t2 = Task(raw_text="y", title="改文案", project="demo", state="deployed", source="dingtalk")
    store.save(t2)
    p.approve(t2.id)
    t2 = store.get(t2.id)
    assert t2.state == "ready_to_deliver" and t2.test_passed is None


def test_multi_repo_requirement_is_split(tmp_path, monkeypatch):
    issues = []
    monkeypatch.setattr("devflow.pipeline.github.create_issue",
                        lambda repo, *a, **k: issues.append(repo) or (len(issues), f"https://x/{repo}/issues/{len(issues)}"))
    cfg, store, p = make(tmp_path, gates=GatesCfg(code=True), ai=FakeAI(project="demo", extra=["demo-api", "demo", "nope"]))
    t = p.ingest("加导出按钮，需要新接口", source="wechat")
    tasks = store.list()
    assert len(tasks) == 2
    main, sib = store.get(1), store.get(2)
    assert main.project == "demo" and main.title.endswith("（demo）") and "配套仓库：demo-api" in main.description
    assert sib.project == "demo-api" and sib.meta["sibling_of"] == 1 and sib.title.endswith("（demo-api）")
    assert main.state == "issue" and sib.state == "issue"
    assert sorted(issues) == ["o/r", "o/r-api"]


def test_group_hint_and_screenshots(tmp_path, monkeypatch):
    from PIL import Image

    monkeypatch.setattr("devflow.pipeline.github.create_issue", lambda *a, **k: (1, "https://x/1"))
    ai = FakeAI(project="demo")  # AI 选了 group 外的仓库
    cfg, store, p = make(tmp_path, gates=GatesCfg(code=True), ai=ai)
    shot = tmp_path / "shot.png"
    Image.new("RGB", (20, 20), "red").save(shot)
    t = p.ingest("首页加个按钮", source="wechat", group="demo", attachments=[str(shot)])
    assert ai.last_hint == "demo" and len(ai.last_images) == 1
    assert t.project == "demo-api"  # group demo 只有 demo-api 一个成员 → 直接用
    assert t.attachments == [str(shot)]
    assert "shot.png" in Pipeline._coder_prompt(t, cfg.project("demo-api"))
    assert cfg.display_name("demo-api") == "demo › api" and cfg.choices()[1]["value"] == "group:demo"


def test_clipboard_image_attaches_to_recent_or_next(tmp_path):
    cfg, store, p = make(tmp_path, ai=FakeAI(project=""))
    p.on_clipboard_image(b"not-really-png", "wechat")  # 没有最近任务 → 挂起，等文字
    t = p.on_clipboard_text("张总：登录页报错", "wechat")
    assert len(t.attachments) == 1 and t.source == "wechat"
    p.on_clipboard_image(b"not-really-png", "wechat")  # 3 分钟内刚收的任务 → 直接附上
    assert len(store.get(t.id).attachments) == 2
    p.on_clipboard_image(b"not-really-png", "dingtalk")  # 来源不同 → 不附到微信任务上
    assert len(store.get(t.id).attachments) == 2


def test_clipboard_app_filter():
    from devflow.inbox.clipboard import accept_app, source_for

    apps = ["WeChat.exe", "DingTalk.exe"]
    assert accept_app("wechat.exe", apps) and accept_app("DingTalk.exe", apps)
    assert not accept_app("Code.exe", apps) and not accept_app("chrome.exe", apps)
    assert accept_app("Code.exe", []) and accept_app("", apps)  # 不限制 / 识别失败 → 收
    assert source_for("DingTalk.exe") == "dingtalk" and source_for("WeChat.exe") == "wechat"


def test_investigate_flow_reads_only(tmp_path, monkeypatch):
    from devflow.integrations.claude_code import CoderResult

    calls = []
    monkeypatch.setattr("devflow.pipeline.github.prepare_worktree", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.remove_worktree", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.delete_local_branch", lambda *a, **k: None)
    monkeypatch.setattr("devflow.pipeline.github.create_issue", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该建 Issue")))
    monkeypatch.setattr("devflow.pipeline.run_claude",
                        lambda prompt, **k: calls.append(k) or CoderResult(ok=True, result="**结论**：已解决。依据：commit abc123", cost_usd=0.1, num_turns=3))
    monkeypatch.setattr("devflow.pipeline.Pipeline._push_draft_to_clipboard", lambda self, t: None)
    cfg, store, p = make(tmp_path, ai=FakeAI(project="demo", task_type="investigate"))
    t = p.ingest("这个问题是否已经解决了？帮我看看", source="wechat")
    assert t.is_investigation and t.state == "ready_to_deliver" and t.issue_number is None
    assert "已解决" in t.report and t.release_note == t.report and "已经解决" in t.reply_draft
    assert "Edit" not in calls[0]["allowed_tools"] and "Write" not in calls[0]["allowed_tools"]
    assert store.get(t.id).approve_label == "发送给客户"


def test_retriage_and_ignore_guard(tmp_path):
    cfg, store, p = make(tmp_path, ai=FakeAI(project=""))
    t = p.ingest("加导出按钮", source="wechat")
    p.update(t.id, project="demo", title="改过的标题")
    assert p.retriage(t.id) == "triage"
    t2 = store.get(t.id)
    assert t2.state == "todo" and t2.project == "" and t2.title == "加导出按钮" and t2.raw_text == "加导出按钮"
    p.ignore(t.id)
    p.run("triage", t.id)  # 已忽略的任务，排队中的步骤不再执行
    assert store.get(t.id).state == "ignored"


def test_resume_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr("devflow.pipeline.github.create_issue", lambda *a, **k: (5, "https://x/5"))
    cfg, store, p = make(tmp_path, gates=GatesCfg(code=True))
    for state in ("inbox", "todo", "issue", "ready_to_merge"):
        store.save(Task(raw_text="x", title="t", project="demo", state=state))
    assert p.resume() == 2  # inbox→triage、todo→create_issue；issue 有 code 门禁、ready_to_merge 由轮询处理
    assert store.get(1).state == "issue" and store.get(2).state == "issue"


def test_config_hot_reload(tmp_path):
    import os
    import time

    from devflow.config import load_config

    p = tmp_path / "config.yaml"
    p.write_text("projects:\n  - name: a\n", encoding="utf-8")
    cfg = load_config(str(p))
    assert [x.name for x in cfg.projects] == ["a"] and cfg.reload_if_changed() is False
    p.write_text("gates:\n  merge: false\nprojects:\n  - name: a\n  - name: b\n", encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))  # 保证 mtime 变化
    assert cfg.reload_if_changed() is True
    assert [x.name for x in cfg.projects] == ["a", "b"] and cfg.gates.merge is False
    p.write_text("projects: [\n", encoding="utf-8")  # 写坏了 -> 沿用旧配置
    os.utime(p, (time.time() + 10, time.time() + 10))
    assert cfg.reload_if_changed() is False and len(cfg.projects) == 2


def test_resolve_cleans_up(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr("devflow.pipeline.github.pr_status", lambda repo, n: {"state": "OPEN"})
    monkeypatch.setattr("devflow.pipeline.github.close_pr", lambda repo, n, c="": calls.append(("pr", n)))
    monkeypatch.setattr("devflow.pipeline.github.delete_remote_branch", lambda p, b: calls.append(("branch", b)) or True)
    monkeypatch.setattr("devflow.pipeline.github.delete_local_branch", lambda p, b: None)
    monkeypatch.setattr("devflow.pipeline.github.close_issue", lambda repo, n, c="": calls.append(("issue", n)))
    cfg, store, p = make(tmp_path)
    t = Task(raw_text="x", title="t", project="demo", state="failed", issue_number=5, pr_number=9, branch="devflow/1-x")
    store.save(t)
    done = p.resolve(t.id, note="已手动修复")
    assert calls == [("pr", 9), ("branch", "devflow/1-x"), ("issue", 5)] and len(done) == 3
    t = store.get(t.id)
    assert t.state == "delivered" and t.is_done and "已手动修复" in t.events[-1].msg


def test_ignore_and_digest(tmp_path, monkeypatch):
    cfg, store, p = make(tmp_path, ai=FakeAI(project=""))
    t = p.ingest("加导出按钮", source="wechat")
    title, body = build_digest(store, cfg)
    assert "等你处理 1 个" in body and "#1" in body
    p.ignore(t.id)
    assert store.get(t.id).state == "ignored"
    _, body = build_digest(store, cfg)
    assert "未完成 0 个" in body

"""交付流水线：
收件箱 → AI 分诊 → GitHub Issue → Claude Code 编码 → PR / CI → 合并 → 部署 → AI 测试 → Release Note → 回复客户
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import httpx

from .ai import AI
from .config import Config, ProjectCfg
from .integrations import browser_test, github
from .integrations.claude_code import CoderResult, find_claude, run_claude
from .integrations.dingtalk import DingTalkAPI
from .models import Task, now_iso
from .notify import Notifier, toast
from .store import Store

log = logging.getLogger("devflow.pipeline")


def _slug(text: str, n: int = 24) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:n] or "task"


def _minutes_since(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        t = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    now = dt.datetime.now(t.tzinfo) if t.tzinfo else dt.datetime.now()
    return (now - t).total_seconds() / 60


class Pipeline:
    def __init__(self, cfg: Config, store: Store, ai: AI | None = None,
                 notifier: Notifier | None = None, sync: bool = False):
        self.cfg = cfg
        self.store = store
        self.ai = ai or AI(cfg)
        self.notifier = notifier or Notifier(cfg)
        self.dingtalk = DingTalkAPI(cfg.dingtalk.client_id, cfg.dingtalk.client_secret, cfg.dingtalk.robot_code)
        self.clipboard_ignore: set[str] = set()  # 与 ClipboardWatcher 共享
        self._pending_images: list[tuple[float, str]] = []  # 剪贴板里先到的截图，等配套文字
        self.sync = sync  # True: enqueue 立即执行（CLI 无服务时用）
        # 两条队列：分诊/测试/写文案几十秒就完；写代码要十几分钟，不能让前者排在后者后面
        self._fast_q: "queue.Queue[tuple[str, int]]" = queue.Queue()
        self._coder_q: "queue.Queue[tuple[str, int]]" = queue.Queue()
        self._stop = threading.Event()

    CODER_ACTIONS = {"code", "fix_ci", "investigate"}

    # 每个步骤只在这些状态下才会执行（防止队列里的旧作业在任务被改动后串步骤）
    STEP_STATES = {
        "triage": {"inbox"},
        "create_issue": {"todo"},
        "code": {"issue", "coding"},
        "fix_ci": {"pr_open", "ci_failed", "coding"},
        "investigate": {"todo", "investigating"},
        "merge": {"ready_to_merge"},
        "test": {"deployed", "testing", "test_failed"},
        "draft_delivery": {"verified", "test_failed", "investigated", "ready_to_deliver"},
        "deliver": {"ready_to_deliver"},
    }

    # 服务重启后按状态接着跑
    RESUME_ACTIONS = {
        "inbox": "triage", "coding": "code", "investigating": "investigate", "deployed": "test",
        "testing": "test", "verified": "draft_delivery", "investigated": "draft_delivery",
    }

    def resume(self) -> int:
        n = 0
        for t in sorted((t for t in self.store.list(limit=500) if not t.is_done), key=lambda t: t.id or 0):
            action = self.RESUME_ACTIONS.get(t.state)
            last = t.events[-1].msg if t.events else ""
            if t.state == "todo" and t.project and not self.cfg.gates.triage:
                action = "investigate" if t.is_investigation else "create_issue"
            elif t.state == "issue" and not self.cfg.gates.code:
                action = "code"
            elif t.state == "ready_to_merge" and last.startswith("👤 确认"):
                action = "merge"  # 你点过合并但服务重启前没来得及执行
            elif t.state == "ready_to_deliver" and last.startswith("👤 确认"):
                action = "deliver"
            if not action:
                continue
            t.log(f"服务重启，继续执行 {action}")
            self.store.save(t)
            self.enqueue(action, t.id)
            n += 1
        return n

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        for name, target in (
            ("pipeline-fast", lambda: self._worker(self._fast_q)),
            ("pipeline-coder", lambda: self._worker(self._coder_q)),
            ("pipeline-poller", self._poll_loop),
        ):
            threading.Thread(target=target, name=name, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def enqueue(self, action: str, task_id: int) -> None:
        if self.sync:
            self.run(action, task_id)
        elif action in self.CODER_ACTIONS:
            self._coder_q.put((action, task_id))
        else:
            self._fast_q.put((action, task_id))

    def _worker(self, q: "queue.Queue[tuple[str, int]]") -> None:
        while not self._stop.is_set():
            try:
                action, task_id = q.get(timeout=1)
            except queue.Empty:
                continue
            self.run(action, task_id)

    def run(self, action: str, task_id: int) -> Task:
        """同步执行一步。"""
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"任务 #{task_id} 不存在")
        if task.state == "ignored":
            log.info("任务 #%s 已忽略，跳过 %s", task_id, action)
            return task
        allowed = self.STEP_STATES.get(action)
        if allowed and task.state not in allowed:  # 排队期间任务被重新分析/忽略/推进了，旧作业作废
            log.info("任务 #%s 现在是「%s」，跳过过期的 %s", task_id, task.label, action)
            return task
        fn = getattr(self, f"step_{action}")
        log.info("任务 #%s: %s", task_id, action)
        try:
            fn(task)
        except Exception as e:  # noqa: BLE001
            log.exception("任务 #%s 步骤 %s 失败", task_id, action)
            task = self.store.get(task_id) or task
            task.error = f"{action}: {e}"
            task.set_state("failed", f"❌ {action} 失败：{e}")
            self.store.save(task)
            self.notifier.me(f"❌ 任务 #{task.id} 出错", f"{task.title or task.raw_text[:30]}\n{task.error[:300]}")
        return self.store.get(task_id)

    # ------------------------------------------------------------ 入口
    def ingest(self, text: str, source: str = "manual", meta: dict | None = None,
               project: str = "", customer: str = "", group: str = "",
               attachments: list[str] | None = None) -> Task:
        self.cfg.reload_if_changed()
        task = Task(source=source, meta=meta or {}, raw_text=text.strip(), project=project,
                    customer=customer, attachments=list(attachments or []))
        if group:
            task.meta["group"] = group  # 用户只选了产品，让 AI 决定改哪个仓库
        shots = f"，{len(task.attachments)} 张截图" if task.attachments else ""
        task.log(f"收到来自 {source} 的消息（{len(task.raw_text)} 字{shots}）")
        self.store.save(task)
        self.enqueue("triage", task.id)
        return self.store.get(task.id)

    def on_dingtalk_message(self, text: str, meta: dict) -> str:
        """钉钉机器人回调：立刻建收件箱任务并回一句，分析结果稍后再回。"""
        nick = meta.get("sender_nick") or ""
        attachments = meta.pop("attachments", None) or []
        task = self.ingest(text, source="dingtalk", meta=meta, customer=nick, attachments=attachments)
        return f"收到，已记录为 #{task.id}，正在分析需求，稍后回复你。"

    def on_clipboard_text(self, text: str, source: str = "wechat") -> Task:
        """剪贴板里复制了文字：连同刚才复制的截图一起建任务。"""
        return self.ingest(text, source=source, attachments=self._take_pending_images())

    def on_clipboard_image(self, png: bytes, source: str = "wechat") -> None:
        """剪贴板里出现图片（客户截图）：附到 3 分钟内刚收的同来源任务上；否则留给接下来复制的文字（10 分钟内有效）。"""
        path = self._save_attachment(png, "clip")
        recent = [t for t in self.store.list(["inbox", "todo"], limit=5)
                  if t.source == source and _minutes_since(t.created_at) <= 3]
        if recent:
            t = recent[0]
            t.attachments.append(path)
            t.log("附加了一张剪贴板截图")
            self.store.save(t)
            log.info("剪贴板截图已附到任务 #%s", t.id)
            return
        self._pending_images.append((time.time(), path))
        log.info("收到剪贴板截图，等待配套文字: %s", path)

    def _take_pending_images(self) -> list[str]:
        now = time.time()
        paths = [p for ts, p in self._pending_images if now - ts <= 600]
        self._pending_images = []
        return paths

    def _save_attachment(self, data: bytes, prefix: str, ext: str = "png") -> str:
        d = self.cfg.data_path / "attachments"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{prefix}-{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}.{ext}"
        p.write_bytes(data)
        return str(p)

    @staticmethod
    def _load_images(paths: list[str], limit: int = 4) -> list[bytes]:
        """读取截图并统一转成 PNG（缩到 1600px 内），给多模态分诊用。"""
        out: list[bytes] = []
        for p in paths[:limit]:
            try:
                from PIL import Image

                img = Image.open(p)
                img.thumbnail((1600, 1600))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, "PNG")
                out.append(buf.getvalue())
            except Exception as e:  # noqa: BLE001
                log.warning("读取截图失败 %s: %s", p, e)
        return out

    # ------------------------------------------------------------ steps
    def step_triage(self, task: Task) -> None:
        group = task.meta.get("group", "")
        r = self.ai.triage(task.raw_text, task.source, images=self._load_images(task.attachments),
                           group_hint=group)
        task.confidence = r.confidence
        task.triage_reason = r.reason
        if not r.is_requirement:
            task.set_state("not_task", f"AI 判定非需求（{r.confidence:.0%}）：{r.reason}")
            self.store.save(task)
            if task.source == "dingtalk":
                self._reply_source(task, f"这条看起来不是开发需求（{r.reason}），先不建任务。如需处理请再说明一下。")
            return
        task.title = r.title or task.raw_text[:20]
        task.description = r.description
        task.acceptance = r.acceptance_criteria
        task.questions = r.questions
        task.priority = r.priority
        task.customer = task.customer or r.customer
        members = [p.name for p in self.cfg.group_members(group)] if group else []
        if not task.project:
            cand = r.project if self.cfg.project(r.project) else ""
            if group and cand not in members:  # 用户指定了产品，AI 选到别处 → 只有一个仓库就直接用，否则等人选
                cand = members[0] if len(members) == 1 else ""
            task.project = cand
        task.deadline = r.deadline
        task.task_type = r.task_type
        # 同一产品多个仓库（前端 + 后端）：拆成配套任务，各自走完整流程（检查类不拆）
        extras = [] if task.is_investigation else [
            p for p in dict.fromkeys(r.extra_projects)
            if self.cfg.project(p) and p != task.project and (not group or p in members)]
        if extras and task.project:
            task.title = f"{task.title}（{task.project}）"
            task.description += (f"\n\n> 本任务只负责仓库 {task.project} 的部分；"
                                 f"配套仓库：{', '.join(extras)}（另有各自的任务）。")
        task.set_state("todo", f"AI 判定为需求（{r.confidence:.0%}）：{r.reason}")
        self.store.save(task)
        siblings = [self._spawn_sibling(task, p) for p in extras] if task.project else []

        summary = self._task_summary(task)
        if siblings:
            summary += "\n\n配套任务：" + "、".join(f"#{s.id}（{s.project}）" for s in siblings)
        self._reply_source(task, f"已创建任务 **#{task.id}**\n\n{summary}")
        if not task.project:
            self.notifier.me(f"📥 新任务 #{task.id} 需要你选择项目", summary)
            return
        if self.cfg.gates.triage:
            self.notifier.me(f"📥 新任务 #{task.id} 等你确认", summary)
            return
        if task.is_investigation:
            self.enqueue("investigate", task.id)
            return
        self.step_create_issue(task)
        for s in siblings:
            self.enqueue("create_issue", s.id)

    def step_investigate(self, task: Task) -> None:
        """检查类需求：Claude Code 只读地查代码、提交记录、测试和线上环境，给出结论；不改代码、不开 PR。"""
        proj = self._project(task)
        task.set_state("investigating", "Claude Code 只读检查代码、提交记录与线上环境")
        self.store.save(task)
        wt = self._worktree(task)
        branch = f"devflow/inv-{task.id}"
        github.prepare_worktree(proj.repo_path, proj.default_branch, branch, wt)
        try:
            c = self.cfg.coder
            res = run_claude(
                self._investigate_prompt(task, proj), cwd=wt, exe=find_claude(c.claude_path),
                allowed_tools="Read,Glob,Grep,LS,WebFetch,Bash(git *),Bash(python *),Bash(pytest *),Bash(npm *),Bash(npx *)",
                max_turns=min(c.max_turns, 40), model=c.model, timeout_s=c.timeout_minutes * 60,
            )
            if not res.ok:
                raise RuntimeError(f"Claude Code 未完成：{res.error_summary}")
            task.report = res.result[-6000:]
            task.coder_cost += res.cost_usd
            task.set_state("investigated", f"检查完成（{res.num_turns} 轮，${res.cost_usd:.2f}）")
        finally:
            github.remove_worktree(proj.repo_path, wt)
            github.delete_local_branch(proj.repo_path, branch)
        self.store.save(task)
        self.enqueue("draft_delivery", task.id)

    def _spawn_sibling(self, task: Task, project: str) -> Task:
        base_title = task.title.rsplit("（", 1)[0]
        sib = Task(
            source=task.source, meta={**task.meta, "sibling_of": task.id}, raw_text=task.raw_text,
            title=f"{base_title}（{project}）",
            description=task.description.split("\n\n> 本任务只负责")[0]
            + f"\n\n> 本任务只负责仓库 {project} 的部分；主任务 #{task.id}（{task.project}）。",
            acceptance=task.acceptance, questions=task.questions, priority=task.priority,
            customer=task.customer, project=project, deadline=task.deadline,
            confidence=task.confidence, triage_reason=task.triage_reason,
            attachments=list(task.attachments),
        )
        sib.set_state("todo", f"由任务 #{task.id} 拆分（同一需求涉及多个仓库）")
        self.store.save(sib)
        task.log(f"拆分出配套任务 #{sib.id}（{project}）")
        self.store.save(task)
        return sib

    def step_create_issue(self, task: Task) -> None:
        proj = self._project(task)
        num, url = github.create_issue(proj.github_repo, f"[{task.priority}] {task.title}",
                                       self._issue_body(task), proj.labels)
        task.issue_number, task.issue_url = num, url
        task.set_state("issue", f"已创建 Issue #{num}: {url}")
        self.store.save(task)
        if self.cfg.gates.code:
            self.notifier.me(f"📝 Issue 已建 #{task.id}", f"{url}\n等你确认后开始自动编码")
            return
        self.enqueue("code", task.id)

    def step_code(self, task: Task) -> None:
        proj = self._project(task)
        task.branch = task.branch or f"devflow/{task.id}-{_slug(task.title)}"
        task.set_state("coding", f"开始自动编码（Claude Code），分支 {task.branch}")
        self.store.save(task)
        wt = self._worktree(task)
        github.prepare_worktree(proj.repo_path, proj.default_branch, task.branch, wt)
        try:
            res = self._run_coder(wt, self._coder_prompt(task, proj))
            task.coder_summary = res.result[-4000:]
            task.coder_cost += res.cost_usd
            task.log(f"Claude Code 完成：{res.num_turns} 轮，${res.cost_usd:.2f}")
            if github.has_changes(wt):
                task.head_sha = github.commit_and_push(
                    wt, task.branch, f"{task.title} (#{task.issue_number})\n\nDevFlow task #{task.id}")
            elif github.commit_log(wt, proj.default_branch):
                task.head_sha = github.push_branch(wt, task.branch)  # Claude 自己 commit 了
            else:
                raise RuntimeError("Claude 没有产生任何代码改动，请查看 coder_summary")
            num, url = github.create_pr(proj.github_repo, proj.default_branch, task.branch,
                                        f"[{task.priority}] {task.title}", self._pr_body(task))
            task.pr_number, task.pr_url = num, url
            task.pr_opened_at = now_iso()
            task.set_state("pr_open", f"已提 PR #{num}: {url}")
        finally:
            github.remove_worktree(proj.repo_path, wt)
        self.store.save(task)
        self.notifier.me(f"🔀 PR 已开 #{task.id}", f"{task.title}\n{task.pr_url}\n等待 CI…", desktop=False)

    def step_fix_ci(self, task: Task) -> None:
        proj = self._project(task)
        logs = github.failed_run_logs(proj.github_repo, task.branch)
        task.ci_fix_attempts += 1
        task.set_state("coding", f"CI 失败，第 {task.ci_fix_attempts} 次自动修复")
        self.store.save(task)
        wt = self._worktree(task)
        github.prepare_worktree(proj.repo_path, proj.default_branch, task.branch, wt, from_existing=True)
        try:
            res = self._run_coder(wt, self._fix_prompt(task, logs))
            task.coder_summary += "\n\n## CI 修复\n" + res.result[-2000:]
            task.coder_cost += res.cost_usd
            if github.has_changes(wt):
                task.head_sha = github.commit_and_push(wt, task.branch, f"fix: CI 修复 (#{task.issue_number})")
            else:
                raise RuntimeError("自动修复没有产生改动")
            task.pr_opened_at = now_iso()
            task.set_state("pr_open", "已推送修复，等待 CI")
        finally:
            github.remove_worktree(proj.repo_path, wt)
        self.store.save(task)

    def step_merge(self, task: Task) -> None:
        proj = self._project(task)
        sha = github.merge_pr(proj.github_repo, task.pr_number)
        task.merge_sha = sha
        task.merged_at = now_iso()
        task.set_state("deploying", f"已合并到 {proj.default_branch}（{sha[:7]}），等待部署")
        self.store.save(task)

    def step_test(self, task: Task) -> None:
        proj = self._project(task)
        if not proj.deploy_url:
            task.test_passed = None
            task.test_report = "未配置 deploy_url，跳过自动测试"
            task.set_state("verified", task.test_report)
            self.store.save(task)
            self.enqueue("draft_delivery", task.id)
            return
        task.set_state("testing", "打开部署地址截图，AI 判定中")
        self.store.save(task)
        shot = self.cfg.data_path / "screenshots" / f"task-{task.id}.png"
        info = browser_test.run_browser_check(proj.deploy_url, shot)
        task.screenshot_path = str(shot) if shot.exists() else ""
        png = shot.read_bytes() if shot.exists() else None
        verdict = self.ai.judge_test(task, proj, info, png)
        task.test_passed = verdict.passed
        task.test_report = verdict.summary
        if verdict.issues:
            task.test_report += "\n" + "\n".join(f"- {i}" for i in verdict.issues)
        if verdict.passed:
            task.set_state("verified", f"✅ AI 测试通过（把握 {verdict.confidence:.0%}）")
        else:
            task.set_state("test_failed", f"⚠️ AI 测试未通过：{verdict.summary[:200]}")
            self.notifier.me(f"⚠️ #{task.id} AI 测试未通过", f"{task.title}\n{verdict.summary[:300]}")
        self.store.save(task)
        self.enqueue("draft_delivery", task.id)

    def step_draft_delivery(self, task: Task) -> None:
        proj = self.cfg.project(task.project)
        if task.is_investigation:
            task.release_note = task.report
            task.reply_draft = self.ai.investigation_reply(task, proj)
            task.set_state("ready_to_deliver", "已根据检查结论生成客户回复草稿")
            self.store.save(task)
        else:
            commits = files = ""
            if proj and task.pr_number:
                try:
                    commits, files = github.pr_summary(proj.github_repo, task.pr_number)
                except Exception as e:  # noqa: BLE001
                    log.warning("读取 PR 摘要失败: %s", e)
            task.release_note = self.ai.release_note(task, commits, files)
            task.reply_draft = self.ai.customer_reply(task, proj)
            task.set_state("ready_to_deliver", "已生成更新说明和客户回复草稿")
            self.store.save(task)
            if proj and task.issue_number:
                github.comment_issue(proj.github_repo, task.issue_number,
                                     f"## 更新说明\n{task.release_note}\n\n## 自动测试\n{task.test_report}")
        auto = (not self.cfg.gates.deliver) and task.test_passed is not False and task.source == "dingtalk"
        if auto:
            self.step_deliver(task)
            return
        self._push_draft_to_clipboard(task)
        self.notifier.me(
            f"📨 #{task.id} 待发送客户",
            f"{task.title}\n回复草稿已复制到剪贴板。确认/修改：{self.cfg.dashboard_url}/task/{task.id}",
        )

    def step_deliver(self, task: Task) -> None:
        proj = self.cfg.project(task.project)
        if not task.reply_draft:
            raise RuntimeError("还没有回复草稿")
        if task.source == "dingtalk" and (task.meta.get("session_webhook") or self.dingtalk.configured):
            if not self.dingtalk.reply(task.meta, task.title, task.reply_draft):
                raise RuntimeError("钉钉发送失败（sessionWebhook 已过期且未配置应用凭证/权限）")
            task.log("已通过钉钉发送给客户")
        else:
            self._push_draft_to_clipboard(task)
            task.log("回复已复制到剪贴板，请粘贴到微信发送")
        task.delivered_at = now_iso()
        task.set_state("delivered", "🎉 已交付")
        self.store.save(task)
        if proj and task.issue_number:
            github.close_issue(proj.github_repo, task.issue_number, "已交付客户（DevFlow）")

    # ------------------------------------------------------------ 轮询 CI / 部署
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001
                log.exception("轮询出错")
            self._stop.wait(self.cfg.pipeline.poll_seconds)

    def poll_once(self) -> None:
        self.cfg.reload_if_changed()
        for task in self.store.list(["pr_open"]):
            try:
                self._check_ci(task)
            except Exception as e:  # noqa: BLE001
                log.warning("检查 CI #%s 失败: %s", task.id, e)
        for task in self.store.list(["deploying"]):
            try:
                self._check_deploy(task)
            except Exception as e:  # noqa: BLE001
                log.warning("检查部署 #%s 失败: %s", task.id, e)

    def _check_ci(self, task: Task) -> None:
        proj = self.cfg.project(task.project)
        if not proj or not task.pr_number:
            return
        st = github.pr_status(proj.github_repo, task.pr_number)
        if st["merged"]:
            task.merge_sha, task.merged_at = st["merge_sha"], now_iso()
            task.set_state("deploying", "PR 已在 GitHub 上合并，等待部署")
            self.store.save(task)
            return
        if st["state"] == "CLOSED":
            task.set_state("failed", "PR 被关闭（未合并）")
            self.store.save(task)
            return
        if st["checks"] in ("success", "none"):
            task.set_state("ready_to_merge", "CI 通过" if st["checks"] == "success" else "仓库没有配置 CI 检查")
            self.store.save(task)
            if self.cfg.gates.merge:
                self.notifier.me(f"✅ #{task.id} CI 通过，等你确认合并", f"{task.title}\n{task.pr_url}")
            else:
                self.enqueue("merge", task.id)
        elif st["checks"] == "failure":
            names = ", ".join(st["failed"])
            if task.ci_fix_attempts < self.cfg.pipeline.ci_fix_attempts:
                task.log(f"CI 失败（{names}），排队自动修复")
                self.store.save(task)
                self.enqueue("fix_ci", task.id)
            else:
                task.set_state("ci_failed", f"CI 失败：{names}")
                self.store.save(task)
                self.notifier.me(f"❌ #{task.id} CI 失败", f"{task.title}\n{task.pr_url}")
        elif _minutes_since(task.pr_opened_at) > self.cfg.pipeline.ci_timeout_minutes:
            task.set_state("ci_failed", "CI 超时未完成")
            self.store.save(task)

    def _check_deploy(self, task: Task) -> None:
        proj = self.cfg.project(task.project)
        if not proj:
            return
        waited = _minutes_since(task.merged_at)
        timeout = self.cfg.pipeline.deploy_timeout_minutes
        if proj.deploy_workflow:
            run = github.find_workflow_run(proj.github_repo, proj.deploy_workflow, task.merge_sha) if task.merge_sha else None
            if run:
                task.deploy_run_url = run.get("url", "")
                if run.get("status") != "completed":
                    if waited > timeout:
                        task.set_state("failed", f"部署超过 {timeout} 分钟未完成：{task.deploy_run_url}")
                        self.store.save(task)
                    return
                if run.get("conclusion") != "success":
                    task.set_state("failed", f"部署工作流失败：{task.deploy_run_url}")
                    self.store.save(task)
                    self.notifier.me(f"❌ #{task.id} 部署失败", task.deploy_run_url)
                    return
            elif waited > timeout:
                task.set_state("failed", f"合并后 {timeout} 分钟内没有找到 {proj.deploy_workflow} 的运行记录")
                self.store.save(task)
                return
            else:
                return
        elif waited * 60 < proj.deploy_wait_seconds:
            return
        if proj.health_url:
            ok, detail = self._health(proj.health_url)
            if not ok:
                if waited > timeout:
                    task.set_state("failed", f"部署后健康检查失败：{detail}")
                    self.store.save(task)
                return
            task.log(f"健康检查通过：{detail}")
        task.set_state("deployed", "部署完成")
        self.store.save(task)
        self.enqueue("test", task.id)

    # ------------------------------------------------------------ 用户操作
    APPROVE_ACTIONS = {
        "todo": "create_issue", "issue": "code", "ready_to_merge": "merge", "deployed": "test",
        "verified": "draft_delivery", "test_failed": "draft_delivery", "ready_to_deliver": "deliver",
    }
    RETRY_ACTIONS = {
        "inbox": "triage", "todo": "create_issue", "issue": "code", "coding": "code", "ci_failed": "fix_ci",
        "investigating": "investigate", "investigated": "draft_delivery",
        "ready_to_merge": "merge", "deployed": "test", "testing": "test", "test_failed": "test",
        "verified": "draft_delivery", "ready_to_deliver": "draft_delivery",
    }

    def approve(self, task_id: int) -> str:
        task = self._get(task_id)
        action = self.APPROVE_ACTIONS.get(task.state)
        if task.state == "todo" and task.is_investigation:
            action = "investigate"
        if not action:
            raise ValueError(f"状态「{task.label}」没有可确认的操作")
        if action in ("create_issue", "investigate") and not task.project:
            raise ValueError("请先选择项目")
        task.log(f"👤 确认：{action}")
        self.store.save(task)
        self.enqueue(action, task.id)
        return action

    def retriage(self, task_id: int) -> str:
        """让 AI 重新分析（改了项目配置 / AI 判错类型或项目时用）。保留原文、截图、来源、客户。"""
        task = self._get(task_id)
        fresh = Task(id=task.id, source=task.source, meta=task.meta, raw_text=task.raw_text,
                     attachments=task.attachments, customer=task.customer, events=task.events,
                     created_at=task.created_at)
        if task.issue_url:
            fresh.log(f"原 Issue 保留在 GitHub：{task.issue_url}（不需要的话手动关闭）")
        fresh.set_state("inbox", "👤 重新分析")
        self.store.save(fresh)
        self.enqueue("triage", task.id)
        return "triage"

    def retry(self, task_id: int, note: str = "") -> str:
        task = self._get(task_id)
        state = task.prev_state if task.state == "failed" else task.state
        if note:
            task.description += f"\n\n补充说明（{now_iso()}）：{note}"
        task.error = ""
        if state == "coding" and task.pr_number:
            action = "fix_ci"
        elif state == "todo" and task.is_investigation:
            action = "investigate"
        else:
            action = self.RETRY_ACTIONS.get(state)
        if action is None:  # pr_open / deploying：回到轮询即可
            task.set_state(state or "inbox", "重新进入轮询")
            self.store.save(task)
            return state
        task.set_state(state, f"👤 重试 {action}")
        self.store.save(task)
        self.enqueue(action, task.id)
        return action

    def ignore(self, task_id: int) -> None:
        task = self._get(task_id)
        task.set_state("ignored", "👤 已忽略")
        self.store.save(task)
        proj = self.cfg.project(task.project)
        if proj and task.issue_number:
            github.close_issue(proj.github_repo, task.issue_number, "已忽略（DevFlow）")

    def resolve(self, task_id: int, note: str = "") -> list[str]:
        """你自己把问题解决了：关掉 DevFlow 开的 PR/分支/Issue，任务标记为已交付。返回做了什么。"""
        task = self._get(task_id)
        proj = self.cfg.project(task.project)
        done: list[str] = []
        comment = "已由开发者另行解决" + (f"：{note}" if note else "") + "（DevFlow）"
        if proj and task.pr_number:
            try:
                st = github.pr_status(proj.github_repo, task.pr_number)
                if st["state"] == "OPEN":
                    github.close_pr(proj.github_repo, task.pr_number, comment)
                    done.append(f"关闭 PR #{task.pr_number}")
            except Exception as e:  # noqa: BLE001
                log.warning("关闭 PR #%s 失败: %s", task.pr_number, e)
        if proj and task.branch:
            if github.delete_remote_branch(proj.repo_path, task.branch):
                done.append(f"删除分支 {task.branch}")
            github.delete_local_branch(proj.repo_path, task.branch)
        if proj and task.issue_number:
            github.close_issue(proj.github_repo, task.issue_number, comment)
            done.append(f"关闭 Issue #{task.issue_number}")
        task.delivered_at = now_iso()
        task.set_state("delivered", "👤 手动标记已解决" + (f"：{note}" if note else "") + ("；" + "、".join(done) if done else ""))
        self.store.save(task)
        return done

    EDITABLE = ("title", "project", "customer", "priority", "deadline", "description", "reply_draft", "release_note")

    def update(self, task_id: int, **fields) -> Task:
        task = self._get(task_id)
        changed = []
        for k, v in fields.items():
            if k in self.EDITABLE and v is not None and getattr(task, k) != v:
                setattr(task, k, v)
                changed.append(k)
        if changed:
            task.log("👤 修改了 " + "、".join(changed))
            self.store.save(task)
        return task

    # ------------------------------------------------------------ helpers
    def _get(self, task_id: int) -> Task:
        task = self.store.get(task_id)
        if not task:
            raise KeyError(f"任务 #{task_id} 不存在")
        return task

    def _project(self, task: Task) -> ProjectCfg:
        proj = self.cfg.project(task.project)
        if not proj:
            raise ValueError("任务没有归属项目，请在面板里选择项目")
        if not proj.github_repo:
            raise ValueError(f"项目 {proj.name} 未配置 github_repo")
        if not proj.repo_path:
            raise ValueError(f"项目 {proj.name} 未配置 repo_path")
        if not Path(proj.repo_path).exists():
            try:
                github.ensure_repo(proj.repo_path, proj.github_repo, proj.default_branch)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"项目 {proj.name} 的 repo_path 不存在，自动 clone {proj.github_repo} 也失败：{e}") from e
        return proj

    def _worktree(self, task: Task) -> Path:
        base = Path(self.cfg.coder.worktree_dir) if self.cfg.coder.worktree_dir else Path.home() / ".devflow" / "worktrees"
        if " " in str(base):
            log.warning("worktree 目录 %s 含空格，Claude 的 cd 命令可能被权限拦住，建议改 coder.worktree_dir", base)
        base.mkdir(parents=True, exist_ok=True)
        return base / f"task-{task.id}"

    def _run_coder(self, cwd: Path, prompt: str) -> CoderResult:
        c = self.cfg.coder
        kw = dict(cwd=cwd, exe=find_claude(c.claude_path), allowed_tools=c.allowed_tools, model=c.model,
                  permission_mode="acceptEdits", skip_permissions=c.skip_permissions,
                  timeout_s=c.timeout_minutes * 60)
        res = run_claude(prompt, max_turns=c.max_turns, **kw)
        if not res.ok and res.subtype == "error_max_turns" and res.session_id and c.resume_turns > 0:
            # 轮数用完但会话还在：续跑一次，要求收尾（已做的改动都在 worktree 里，不会丢）
            log.info("Claude Code 达到 %d 轮未完成，续跑 %d 轮收尾", c.max_turns, c.resume_turns)
            more = run_claude(
                "轮数快用完了。请停止探索，用剩下的步骤完成最核心的改动，跑一次相关测试，"
                "然后用中文输出 5-10 行总结：改了哪些文件、哪些验收标准已满足、哪些没做完、如何验证。",
                max_turns=c.resume_turns, resume=res.session_id, **kw,
            )
            more.cost_usd += res.cost_usd
            more.num_turns += res.num_turns
            more.denials = res.denials + more.denials
            res = more
        if not res.ok:
            raise RuntimeError(f"Claude Code 未完成：{res.error_summary}")
        return res

    def _health(self, url: str) -> tuple[bool, str]:
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            return r.status_code < 500, f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            return False, str(e)[:200]

    def _reply_source(self, task: Task, text: str) -> None:
        if task.source == "dingtalk":
            try:
                self.dingtalk.reply(task.meta, "DevFlow", text)
            except Exception as e:  # noqa: BLE001
                log.warning("回复钉钉失败: %s", e)

    def _push_draft_to_clipboard(self, task: Task) -> None:
        try:
            import pyperclip

            self.clipboard_ignore.add(task.reply_draft.strip())
            pyperclip.copy(task.reply_draft)
            toast(f"#{task.id} 回复草稿已复制", "到微信里 Ctrl+V 发给客户即可")
        except Exception as e:  # noqa: BLE001
            log.warning("写剪贴板失败: %s", e)

    # ---- 文案
    @staticmethod
    def _task_summary(task: Task) -> str:
        s = (f"**{task.title}**\n\n"
             f"- 优先级：{task.priority}　项目：{task.project or '未识别'}　客户：{task.customer or '未识别'}　"
             f"截止：{task.deadline or '无'}\n"
             f"- 验收标准 {len(task.acceptance)} 条")
        if task.attachments:
            s += f"　截图 {len(task.attachments)} 张"
        if task.questions:
            s += "\n\n**需要先确认：**\n" + "\n".join(f"- {q}" for q in task.questions)
        return s

    @staticmethod
    def _issue_body(task: Task) -> str:
        acc = "\n".join(f"- [ ] {a}" for a in task.acceptance) or "- [ ] （无）"
        q = ("\n\n## 待确认\n" + "\n".join(f"- {x}" for x in task.questions)) if task.questions else ""
        return (
            f"## 需求描述\n{task.description}\n\n## 验收标准\n{acc}{q}\n\n"
            f"## 元信息\n- 来源：{task.source}　客户：{task.customer or '-'}　优先级：{task.priority}　"
            f"截止：{task.deadline or '-'}\n- DevFlow 任务 #{task.id}\n\n"
            f"<details><summary>客户原话</summary>\n\n```\n{task.raw_text[:4000]}\n```\n</details>"
        )

    @staticmethod
    def _pr_body(task: Task) -> str:
        acc = "\n".join(f"- [ ] {a}" for a in task.acceptance)
        return (
            f"## 需求\n{task.description}\n\n## 验收标准\n{acc}\n\n"
            f"## Claude Code 改动总结\n{task.coder_summary}\n\n"
            f"Closes #{task.issue_number}\n\n_由 DevFlow AI 自动创建（任务 #{task.id}）_"
        )

    @staticmethod
    def _coder_prompt(task: Task, proj: ProjectCfg) -> str:
        acc = "\n".join(f"- {a}" for a in task.acceptance)
        q = ("\n\n## 尚未和客户确认的点（按最合理的方式实现，并在总结里说明你的假设）\n"
             + "\n".join(f"- {x}" for x in task.questions)) if task.questions else ""
        shots = ("\n\n## 客户截图（请先用 Read 工具逐个查看这些图片，里面有页面、报错或标注）\n"
                 + "\n".join(f"- {p}" for p in task.attachments)) if task.attachments else ""
        return f"""你是仓库 {proj.github_repo} 的开发者。当前目录是一个独立的 git worktree（分支 {task.branch}），请在这里实现下面的需求。

# 需求（Issue #{task.issue_number}）：{task.title}
{task.description}

## 验收标准
{acc}{q}

## 客户原话（供参考）
{task.raw_text[:2000]}{shots}

## 要求
1. 先阅读仓库结构和相关代码，遵循现有代码风格、目录约定和注释密度。
2. 只做需求范围内的改动，不要顺手重构无关代码。
3. 如果仓库有测试，为改动补充/更新测试并运行；确保现有测试通过。
4. 不要执行 git commit / git push（由外部流程处理）。
5. 无法完全实现时，做出最合理的部分实现，并明确说明缺什么。
6. 最后用中文输出 5-10 行总结：改了哪些文件、如何验证、有什么风险或需要客户确认的点。这段总结会直接写进 PR 描述。
"""

    @staticmethod
    def _investigate_prompt(task: Task, proj: ProjectCfg) -> str:
        points = "\n".join(f"- {a}" for a in task.acceptance) or "- 客户问题是否已经解决 / 当前状态如何"
        shots = ("\n\n## 客户截图（请先用 Read 工具逐个查看，里面是客户看到的现象）\n"
                 + "\n".join(f"- {p}" for p in task.attachments)) if task.attachments else ""
        return f"""你是仓库 {proj.github_repo} 的开发者。客户在问一个问题，你需要**只做检查、不改代码**，给出有依据的结论。
当前目录是从 {proj.default_branch} 新建的独立 worktree，代码就是线上/主干最新状态。

# 客户的问题
{task.raw_text[:2000]}

AI 整理：{task.title}
{task.description}

## 需要逐条确认的点
{points}{shots}

## 怎么查
1. 用 git log / git show / grep 找到相关的代码和最近的提交，看现在的实现是什么状态、什么时候改的。
2. 必要时运行现有测试或写临时脚本验证（结束前删掉，不要留下改动）。
3. 线上地址：{proj.deploy_url or '（未配置）'}。可以用 WebFetch 看线上页面/接口是否已生效；访问不了就说明。
4. 不要修改仓库文件，不要 git commit / push。

## 输出（中文，写给开发者本人看，会由他转述给客户）
- **结论**：已解决 / 未解决 / 部分解决 / 无法确定 —— 一句话
- **依据**：对应的提交（hash 和日期）、代码位置、测试或线上表现
- **如果未解决或部分解决**：原因分析 + 建议的修复方案和工作量估计（不要实现）
- **需要客户补充的信息**（如有）
"""

    @staticmethod
    def _fix_prompt(task: Task, logs: str) -> str:
        return f"""分支 {task.branch} 上的 PR #{task.pr_number} 的 CI 失败了。请分析下面的失败日志，修复问题（修改代码或测试），并在本地运行相关测试验证。不要 git commit / git push。
最后用中文简述修复了什么。

# 原需求：{task.title}
{task.description}

# CI 失败日志（节选）
```
{logs or '（未能获取日志，请自行运行测试定位）'}
```
"""

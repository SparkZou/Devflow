from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


STATE_LABELS = {
    "inbox": "收件箱·AI 分析中",
    "not_task": "非需求",
    "todo": "待建 Issue",
    "issue": "已建 Issue",
    "coding": "AI 编码中",
    "investigating": "AI 检查中",
    "investigated": "检查完成",
    "pr_open": "PR 已开·CI 运行中",
    "ci_failed": "CI 失败",
    "ready_to_merge": "待合并（需确认）",
    "deploying": "部署中",
    "deployed": "已部署",
    "testing": "AI 测试中",
    "test_failed": "AI 测试未通过",
    "verified": "验证通过",
    "ready_to_deliver": "待发送客户（需确认）",
    "delivered": "已交付",
    "failed": "出错",
    "ignored": "已忽略",
}

DONE_STATES = {"delivered", "not_task", "ignored"}
# 这些状态下系统一定在等你做决定
ACTION_STATES = {"ready_to_merge", "ready_to_deliver", "ci_failed", "test_failed", "failed"}

# 面板上"确认"按钮的文字
APPROVE_LABELS = {
    "todo": "创建 Issue",
    "issue": "开始编码",
    "ready_to_merge": "合并 PR",
    "deployed": "开始测试",
    "verified": "生成回复",
    "investigated": "生成回复",
    "test_failed": "仍然生成回复",
    "ready_to_deliver": "发送给客户",
}


class Event(BaseModel):
    at: str = Field(default_factory=now_iso)
    msg: str


class Task(BaseModel):
    id: Optional[int] = None
    state: str = "inbox"
    prev_state: str = ""
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)

    source: str = "manual"  # wechat | dingtalk | manual
    meta: dict = Field(default_factory=dict)  # 钉钉会话信息、用户指定的产品 group 等
    raw_text: str = ""
    attachments: list[str] = Field(default_factory=list)  # 客户截图等图片文件路径
    task_type: str = "implement"  # implement = 要改代码 | investigate = 只需检查/确认/回答
    report: str = ""  # 检查类任务：Claude 的检查结论（给你看的）

    title: str = ""
    description: str = ""
    acceptance: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    priority: str = "P2"
    customer: str = ""
    project: str = ""
    deadline: str = ""
    confidence: float = 0.0
    triage_reason: str = ""

    issue_number: Optional[int] = None
    issue_url: str = ""
    branch: str = ""
    pr_number: Optional[int] = None
    pr_url: str = ""
    pr_opened_at: str = ""
    head_sha: str = ""
    merge_sha: str = ""
    merged_at: str = ""
    deploy_run_url: str = ""
    coder_summary: str = ""
    coder_cost: float = 0.0
    ci_fix_attempts: int = 0

    test_passed: Optional[bool] = None
    test_report: str = ""
    screenshot_path: str = ""
    release_note: str = ""
    reply_draft: str = ""
    delivered_at: str = ""

    error: str = ""
    events: list[Event] = Field(default_factory=list)

    # ---- helpers -------------------------------------------------------
    def log(self, msg: str) -> None:
        self.events.append(Event(msg=msg))
        self.updated_at = now_iso()

    def set_state(self, state: str, msg: str = "") -> None:
        if state != self.state:
            self.prev_state = self.state
            self.state = state
        self.log(msg or f"→ {STATE_LABELS.get(state, state)}")

    @property
    def label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def is_done(self) -> bool:
        return self.state in DONE_STATES

    @property
    def overdue(self) -> bool:
        if not self.deadline or self.is_done:
            return False
        try:
            return dt.date.fromisoformat(self.deadline) < dt.date.today()
        except ValueError:
            return False

    @property
    def approve_label(self) -> str:
        if self.state == "todo" and self.task_type == "investigate":
            return "开始检查"
        return APPROVE_LABELS.get(self.state, "")

    @property
    def is_investigation(self) -> bool:
        return self.task_type == "investigate"

    def short(self) -> str:
        return f"#{self.id} [{self.priority}] {self.title or self.raw_text[:30]}"


def needs_action(task: Task, gates) -> bool:
    """任务是否在等开发者点一下。gates 是 config.GatesCfg。"""
    if task.state in ACTION_STATES:
        return True
    if task.state == "todo" and (not task.project or gates.triage):
        return True
    if task.state == "issue" and gates.code:
        return True
    return False

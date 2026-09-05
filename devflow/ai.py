"""AI 大脑：需求分诊、测试判定、Release Note、客户回复。

两种后端：
- anthropic   : Anthropic API（设置了 ANTHROPIC_API_KEY 时）
- claude-code : 本机 Claude Code CLI 的无人值守模式（用你现有的 Claude 登录态，不需要 API Key）
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Literal, Type, TypeVar

from pydantic import BaseModel, Field

from .config import Config, ProjectCfg
from .integrations.claude_code import find_claude, run_claude
from .models import Task

log = logging.getLogger("devflow.ai")
T = TypeVar("T", bound=BaseModel)


class TriageResult(BaseModel):
    is_requirement: bool = Field(description="文本里是否包含需要开发者去处理的事（改代码，或检查/确认/回答技术问题）")
    task_type: Literal["implement", "investigate"] = Field(
        description="implement = 需要新增/修改/修复代码或配置；investigate = 客户只是要求检查、确认、解释或回答"
                    "（如“是否已经解决”“帮我看看”“给出结论”“为什么会这样”），不需要改东西")
    confidence: float = Field(description="0-1 之间")
    reason: str = Field(description="一句话说明判断依据")
    title: str = Field(description="15 字以内的中文标题；非需求时为空字符串")
    description: str = Field(description="开发者视角重写的需求描述，保留所有细节，不编造")
    acceptance_criteria: list[str] = Field(description="3-6 条可验证的验收标准")
    priority: Literal["P0", "P1", "P2", "P3"]
    customer: str = Field(description="客户/发起人称呼，识别不出为空字符串")
    project: str = Field(description="主要改动的候选项目名；无法判断为空字符串")
    extra_projects: list[str] = Field(
        description="同一需求还需要改动的其他候选项目名（例如同一产品前后端分离时的另一端）；没有则空数组")
    deadline: str = Field(description="YYYY-MM-DD；没有则为空字符串")
    questions: list[str] = Field(description="实现前必须向客户确认的阻塞性问题")


class TestVerdict(BaseModel):
    __test__ = False  # 不是 pytest 用例

    passed: bool
    confidence: float = Field(description="0-1 之间")
    summary: str = Field(description="一段话总结看到了什么")
    issues: list[str] = Field(description="发现的问题；无法验证的项以“未能验证：”开头")


def fill_defaults(model_cls: Type[BaseModel], data: dict) -> dict:
    """CLI 后端偶尔会漏掉空字段（如非需求时的 title），按类型补默认值，避免整条任务报错。"""
    import typing

    out = dict(data)
    for name, field in model_cls.model_fields.items():
        if name in out:
            continue
        ann = field.annotation
        origin = typing.get_origin(ann)
        if origin is typing.Literal:
            out[name] = typing.get_args(ann)[0]
        elif origin in (list, typing.List) or ann is list:
            out[name] = []
        elif ann is bool:
            out[name] = False
        elif ann in (int, float):
            out[name] = 0
        else:
            out[name] = ""
    return out


def extract_json(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        return json.loads(m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError(f"AI 输出不是 JSON: {text[:300]}")


# ---------------------------------------------------------------- backends
class AnthropicBackend:
    name = "anthropic"

    def __init__(self, cfg: Config):
        import anthropic

        self.client = anthropic.Anthropic()
        self.model = cfg.ai.model
        self.max_tokens = cfg.ai.max_tokens
        self.extra: dict = {}
        if cfg.ai.fallbacks:
            # 请求被安全策略拒答时，服务端按拒答类别自动换备用模型重跑
            self.extra = dict(
                extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                extra_body={"fallbacks": "default"},
            )

    @staticmethod
    def _blocks(text: str, images: list[bytes] | None) -> list[dict]:
        blocks: list[dict] = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.standard_b64encode(img).decode("ascii")}}
            for img in images or []
        ]
        blocks.append({"type": "text", "text": text})
        return blocks

    def structured(self, system: str, text: str, model_cls: Type[T], images: list[bytes] | None = None) -> T:
        r = self.client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": self._blocks(text, images)}],
            output_format=model_cls,
            **self.extra,
        )
        if r.stop_reason == "refusal":
            raise RuntimeError("模型拒绝了该请求")
        return r.parsed_output

    def text(self, system: str, text: str, images: list[bytes] | None = None) -> str:
        r = self.client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": self._blocks(text, images)}],
            **self.extra,
        )
        if r.stop_reason == "refusal":
            raise RuntimeError("模型拒绝了该请求")
        return "".join(b.text for b in r.content if b.type == "text").strip()


class ClaudeCodeBackend:
    name = "claude-code"

    def __init__(self, cfg: Config):
        self.exe = find_claude(cfg.coder.claude_path)
        if not self.exe:
            raise RuntimeError("找不到 claude 可执行文件，无法使用 claude-code 后端")
        self.model = cfg.ai.cli_model

    @staticmethod
    def _prompt(system: str, text: str, images: list[bytes] | None) -> tuple[str, str, list[str]]:
        parts = [system, "", "<input>", text, "</input>"]
        tools, paths = "", []
        for img in images or []:
            f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            f.write(img)
            f.close()
            paths.append(f.name)
        if paths:
            parts.append("\n请先用 Read 工具查看以下截图文件，再作答：\n" + "\n".join(paths))
            tools = "Read"
        return "\n".join(parts), tools, paths

    def _run(self, prompt: str, tools: str) -> str:
        res = run_claude(prompt, exe=self.exe, allowed_tools=tools, max_turns=6 if tools else 3,
                         model=self.model, timeout_s=600)
        if not res.ok:
            raise RuntimeError(f"claude 返回错误: {res.result[:300]}")
        return res.result

    def structured(self, system: str, text: str, model_cls: Type[T], images: list[bytes] | None = None) -> T:
        schema = json.dumps(model_cls.model_json_schema(), ensure_ascii=False)
        prompt, tools, paths = self._prompt(system, text, images)
        prompt += (
            "\n\n只输出一个 JSON 对象（不要 markdown 代码块、不要解释、不要调用除 Read 以外的工具），"
            "必须符合以下 JSON Schema：\n" + schema
        )
        try:
            out = self._run(prompt, tools)
        finally:
            for p in paths:
                Path(p).unlink(missing_ok=True)
        return model_cls.model_validate(fill_defaults(model_cls, extract_json(out)))

    def text(self, system: str, text: str, images: list[bytes] | None = None) -> str:
        prompt, tools, paths = self._prompt(system, text, images)
        prompt += "\n\n直接输出结果正文，不要解释，不要调用工具。"
        try:
            return self._run(prompt, tools).strip()
        finally:
            for p in paths:
                Path(p).unlink(missing_ok=True)


class NullBackend:
    """既没有 API Key 也找不到 claude 时的占位后端：服务照常启动（面板可用），AI 步骤失败时给出可读提示。"""
    name = "none"
    HINT = "未配置 AI 后端：在 .env 里设置 ANTHROPIC_API_KEY，或安装并登录 Claude Code（devflow doctor 可检查）"

    def __init__(self, cfg: Config):
        pass

    def structured(self, *a, **k):
        raise RuntimeError(self.HINT)

    def text(self, *a, **k):
        raise RuntimeError(self.HINT)


def make_backend(cfg: Config):
    b = cfg.ai.backend
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    if b == "anthropic" or (b == "auto" and has_key):
        return AnthropicBackend(cfg)
    if b == "auto" and not find_claude(cfg.coder.claude_path):
        log.warning(NullBackend.HINT)
        return NullBackend(cfg)
    return ClaudeCodeBackend(cfg)


# ---------------------------------------------------------------- prompts
TRIAGE_SYSTEM = """你是一个软件外包团队的需求分诊助手。开发者同时维护多个项目，客户需求通过微信/钉钉聊天记录发来，经常夹杂寒暄和无关内容。

任务：判断输入文本是否包含需要开发者去处理的事：
- 开发任务（新功能、修改、Bug 修复、配置/数据变更、部署请求等）→ is_requirement=true，task_type=implement
- 检查/确认/回答类（"这个问题是否已经解决了""帮我检查下""给出结论""为什么会这样""现在是什么状态"）→ is_requirement=true，task_type=investigate。
  这类不改代码，由开发者去查代码、提交记录和线上环境后回复结论。客户没明确要求修改时优先判为 investigate。
闲聊、寒暄、纯粹的确认/感谢、通知已完成的事、报价/合同/付款等非开发内容 → is_requirement=false。

如果是需求（两种 task_type 都要填下面这些字段；investigate 的 acceptance_criteria 写"需要确认的点"）：
- title：15 字以内中文标题
- description：以开发者视角重写，保留所有细节（页面、字段、条件、示例、截图描述）。不要编造原文没有的信息。多条需求合并成一个任务时用编号列出。
- acceptance_criteria：3-6 条可验证的验收标准
- priority：P0 线上故障/阻塞客户使用；P1 客户明确在催或有明确截止时间；P2 正常需求；P3 优化建议/锦上添花
- customer：文本中的客户/发起人称呼（如"张总"、群名、公司名）；识别不出填 ""
- project：必须是候选项目名之一，或 ""。依据关键词、客户名、内容判断；不确定就填 ""，不要瞎猜。
- extra_projects：同一个产品可能拆成多个仓库（候选列表里"所属产品"相同的项目，如前端仓库 + 后端仓库）。
  如果需求同时需要改多个仓库（例如新增一个页面且需要新接口），project 填主要的一个，其余填进 extra_projects，
  并在 description 里分别写清楚每个仓库要做什么。只改一端就不要填 extra_projects。
- deadline：YYYY-MM-DD。"明天""周五前""月底"等要换算成具体日期；没有则 ""。
- questions：只列真正阻塞实现、必须先问客户的问题；没有就空数组，不要凑数。

今天是 {today}。消息来源：{source}。
候选项目：
{projects}
{extra}"""

TEST_SYSTEM = """你是 QA 工程师。开发者刚把一个需求部署到测试/生产环境，你要根据截图和页面信息判断这次交付是否明显有问题。
判定原则：
- 页面 HTTP 状态 >= 500、白屏、明显报错、控制台有和本次需求相关的错误 → passed=false
- 截图能直接看到验收标准被满足 → 在 summary 里说明看到了什么
- 仅凭首页截图无法验证的验收项，不要判失败，写进 issues，以"未能验证："开头
- passed=true 的含义是"没有发现明显问题，可以交给客户验收"，不是"全部验收标准都被证实"
confidence 表示你对 passed 结论的把握（0-1）。"""

RELEASE_SYSTEM = """你是技术项目经理，负责把一次开发交付整理成客户能看懂的更新说明（中文 Markdown）。
结构：
## 本次更新
（用客户视角描述做了什么，不要出现文件名、函数名）
## 如何验证
（客户打开哪里、做什么操作、应该看到什么）
## 注意事项
（有就写，没有写"无"）
简洁、准确、不要夸大。不要编造没做的功能。"""

INVESTIGATION_REPLY_SYSTEM = """你是开发者本人。客户问了一个"是否已经解决 / 帮我看看 / 为什么"之类的问题，你已经查过代码、提交记录和线上环境，现在要回复客户。
要求：
- {style_hint}
- 3-8 行：先给结论（已解决 / 未解决 / 部分解决 / 需要更多信息），再说依据（用客户听得懂的话，不要贴代码），最后说下一步（已解决→请验证；未解决→准备怎么修、大概什么时候）
- 检查结论里有"无法确定""需要客户补充"的点要如实问客户
- 不要出现"AI"、"自动"、"Claude"等字眼，不要署名，不要加标题"""

REPLY_SYSTEM = """你是开发者本人，要给客户发一条交付通知。语气：专业、简短、友好，像平时在微信/钉钉里说话。
要求：
- {style_hint}
- 3-8 行，先说做了什么，再给访问地址（如果有），最后请客户验收并反馈
- 如果测试没通过或有"未能验证"的项，要如实说明"哪些请重点验证"
- 如果还有待确认的问题，一起问
- 不要出现"AI"、"自动"等字眼，不要署名，不要加标题"""


class AI:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.backend = make_backend(cfg)
        log.info("AI 后端: %s", self.backend.name)

    # ---- 分诊
    def triage(self, raw_text: str, source: str, images: list[bytes] | None = None,
               group_hint: str = "") -> TriageResult:
        projects = "\n".join(
            f"- {p.name}："
            + (f"所属产品 {p.group}；" if p.group else "")
            + (f"{p.description}；" if p.description else "")
            + f"关键词 {p.keywords or '无'}；客户 {p.customers or '无'}"
            for p in self.cfg.projects
        ) or "（未配置项目）"
        extra = ""
        if group_hint:
            names = "、".join(p.name for p in self.cfg.group_members(group_hint)) or group_hint
            extra += (f"\n用户已指定这条需求属于产品「{group_hint}」：project 和 extra_projects 只能从这些仓库里选：{names}。"
                      "根据需求内容判断该改前端、后端还是两个都改。")
        if images:
            extra += "\n输入附带客户截图，请结合截图（页面、报错、标注、箭头）理解需求，并把截图里能看到的关键信息写进 description。"
        system = TRIAGE_SYSTEM.format(today=dt.date.today().isoformat(), source=source, projects=projects, extra=extra)
        return self.backend.structured(system, raw_text or "（客户只发了截图，没有文字）", TriageResult, images=images)

    # ---- 测试判定
    def judge_test(self, task: Task, project: ProjectCfg | None, page_info: dict,
                   screenshot: bytes | None) -> TestVerdict:
        text = (
            f"# 需求 #{task.id}: {task.title}\n{task.description}\n\n## 验收标准\n"
            + "\n".join(f"- {a}" for a in task.acceptance)
            + f"\n\n## 测试提示\n{(project.test_hints if project else '') or '无'}"
            + f"\n\n## 页面信息\nURL: {page_info.get('url')}\nHTTP: {page_info.get('status')}\n"
            + f"标题: {page_info.get('title')}\n"
            + f"控制台错误: {json.dumps(page_info.get('console_errors') or [], ensure_ascii=False)}\n"
            + f"页面文字节选:\n{page_info.get('text_excerpt') or ''}\n"
            + (f"\n打开页面失败: {page_info.get('error')}" if page_info.get("error") else "")
        )
        return self.backend.structured(TEST_SYSTEM, text, TestVerdict, images=[screenshot] if screenshot else None)

    # ---- Release note
    def release_note(self, task: Task, commits: str, files: str) -> str:
        text = (
            f"# 需求 #{task.id}: {task.title}\n{task.description}\n\n## 验收标准\n"
            + "\n".join(f"- {a}" for a in task.acceptance)
            + f"\n\n## 开发者（Claude Code）的改动总结\n{task.coder_summary or '无'}"
            + f"\n\n## 提交记录\n{commits or '无'}\n\n## 变更文件\n{files or '无'}"
            + f"\n\n## 测试结果\n{task.test_report or '未测试'}"
        )
        return self.backend.text(RELEASE_SYSTEM, text)

    # ---- 检查类任务的客户回复
    def investigation_reply(self, task: Task, project: ProjectCfg | None) -> str:
        style = ("钉钉消息，可以用简单 Markdown（加粗、列表）" if task.source == "dingtalk"
                 else "微信消息，纯文本，不要任何 Markdown 符号")
        text = (
            f"客户称呼：{task.customer or '未知'}\n客户的问题：{task.title}\n{task.description}\n\n"
            f"客户原话：\n{task.raw_text[:1500]}\n\n访问地址：{(project.deploy_url if project else '') or '无'}\n\n"
            f"检查结论（开发者视角，需要转述给客户）：\n{task.report}"
        )
        return self.backend.text(INVESTIGATION_REPLY_SYSTEM.format(style_hint=style), text)

    # ---- 客户回复
    def customer_reply(self, task: Task, project: ProjectCfg | None) -> str:
        style = ("钉钉消息，可以用简单 Markdown（加粗、列表）" if task.source == "dingtalk"
                 else "微信消息，纯文本，不要任何 Markdown 符号")
        result = "通过" if task.test_passed else ("未通过" if task.test_passed is False else "未做自动测试")
        text = (
            f"客户称呼：{task.customer or '未知'}\n需求标题：{task.title}\n"
            f"访问地址：{(project.deploy_url if project else '') or '无'}\n\n"
            f"更新说明：\n{task.release_note}\n\n"
            f"测试结果：{result}\n{task.test_report}\n\n"
            f"待确认问题：{task.questions or '无'}"
        )
        return self.backend.text(REPLY_SYSTEM.format(style_hint=style), text)

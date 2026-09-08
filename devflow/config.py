from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, PrivateAttr, field_validator


class AICfg(BaseModel):
    backend: str = "auto"  # auto | anthropic | claude-code
    model: str = "claude-opus-5"
    cli_model: str = ""
    fallbacks: bool = True
    max_tokens: int = 16000


DEFAULT_ALLOWED_TOOLS = ",".join([
    "Read", "Edit", "Write", "MultiEdit", "Glob", "Grep", "LS", "WebFetch", "PowerShell",
    *(f"Bash({c} *)" for c in (
        "cd", "git", "gh", "python", "py", "pip", "pytest", "uv", "poetry",
        "node", "npm", "npx", "pnpm", "yarn", "tsc", "vite", "next", "jest", "vitest", "eslint", "prettier",
        "dotnet", "cargo", "go", "make", "docker",
        "ls", "dir", "cat", "head", "tail", "grep", "rg", "find", "wc", "echo", "mkdir", "cp", "mv", "touch", "diff",
    )),
])


class CoderCfg(BaseModel):
    claude_path: str = ""
    model: str = ""
    max_turns: int = 120  # 每次工具调用算一轮；大仓库要多留一些
    resume_turns: int = 40  # 达到上限还没完成时，自动续跑一次收尾的轮数
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS
    skip_permissions: bool = False
    timeout_minutes: int = 60
    worktree_dir: str = ""  # 留空 = ~/.devflow/worktrees（路径里不能有空格，否则 Claude 的 cd 命令会被权限拦住）

    @field_validator("allowed_tools", mode="before")
    @classmethod
    def _default_tools(cls, v):
        return v if (v or "").strip() else DEFAULT_ALLOWED_TOOLS


class GatesCfg(BaseModel):
    triage: bool = False
    code: bool = False
    merge: bool = True
    deliver: bool = True


class PipelineCfg(BaseModel):
    poll_seconds: int = 60
    sync_seconds: int = 300  # 多久和 GitHub 对一次账（Issue 被本地关掉 / PR 在网页上合并）
    merge_requires_ci: bool = True  # 自动合并只在 PR 有通过的检查时进行；仓库没配 PR 检查（沙箱不能 build）→ 等你本地 build 后确认
    ci_timeout_minutes: int = 45
    deploy_timeout_minutes: int = 30
    ci_fix_attempts: int = 1


class DingTalkCfg(BaseModel):
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    robot_code: str = ""
    notify_webhook: str = ""
    notify_secret: str = ""


class WeComCfg(BaseModel):
    notify_webhook: str = ""


class ClipboardCfg(BaseModel):
    enabled: bool = True
    min_chars: int = 12
    trigger_prefix: str = ""
    # 只接受在这些程序里复制的内容（按前台窗口的 exe 判断）；空列表 = 任何程序都收
    apps: list[str] = Field(default_factory=lambda: [
        "WeChat.exe", "Weixin.exe", "WeChatAppEx.exe", "WXWork.exe", "DingTalk.exe",
    ])


class AuthCfg(BaseModel):
    """面板登录。浏览器走 Cookie 会话；/api/* 也接受 HTTP Basic（同一组账号），CLI 自动带上。"""
    enabled: bool = True
    username: str = "admin"
    password: str = "admin2026"
    secret: str = ""  # 会话签名密钥；留空 = 首次启动自动生成并保存到 data/.session_secret
    session_days: int = 7


class DashboardCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True


class DigestCfg(BaseModel):
    times: list[str] = Field(default_factory=lambda: ["09:00", "18:00"])
    toast: bool = True


class ProjectCfg(BaseModel):
    name: str
    group: str = ""  # 同一个产品有多个仓库（前端/后端）时填同一个 group 名，面板上合并成一项，AI 自己决定改哪个仓库
    kind: str = "customer"  # customer = 客户项目 | own = 自己的项目（面板分组显示 + 标记）
    description: str = ""  # 给 AI 看的一句话：这个仓库负责什么（前端/后端、技术栈、入口）
    repo_path: str = ""
    github_repo: str = ""
    default_branch: str = "main"
    deploy_url: str = ""
    deploy_workflow: str = ""
    deploy_wait_seconds: int = 120
    health_path: str = "/"
    customers: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    test_hints: str = ""
    labels: list[str] = Field(default_factory=lambda: ["devflow"])
    auto_merge: Optional[bool] = None  # None = 跟随 gates.merge；true = 这个仓库 CI 通过就自动合并；false = 总是等你确认

    @property
    def health_url(self) -> str:
        if not self.deploy_url:
            return ""
        return self.deploy_url.rstrip("/") + "/" + self.health_path.lstrip("/")


class Config(BaseModel):
    ai: AICfg = Field(default_factory=AICfg)
    coder: CoderCfg = Field(default_factory=CoderCfg)
    gates: GatesCfg = Field(default_factory=GatesCfg)
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)
    dingtalk: DingTalkCfg = Field(default_factory=DingTalkCfg)
    wecom: WeComCfg = Field(default_factory=WeComCfg)
    clipboard: ClipboardCfg = Field(default_factory=ClipboardCfg)
    dashboard: DashboardCfg = Field(default_factory=DashboardCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)
    digest: DigestCfg = Field(default_factory=DigestCfg)
    projects: list[ProjectCfg] = Field(default_factory=list)
    data_dir: str = "data"

    _path: Path = PrivateAttr(default=Path("config.yaml"))
    _mtime: float = PrivateAttr(default=0.0)

    @property
    def path(self) -> Path:
        return self._path

    def reload_if_changed(self) -> bool:
        """config.yaml 被改过就原地重新加载（对象身份不变，所有持有 cfg 的组件立刻看到新值）。"""
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            fresh = load_config(str(self._path))
        except Exception as e:  # noqa: BLE001
            logging.getLogger("devflow.config").warning("config.yaml 有改动但解析失败，沿用旧配置：%s", e)
            self._mtime = mtime
            return False
        for name in type(self).model_fields:
            setattr(self, name, getattr(fresh, name))
        self._mtime = mtime
        logging.getLogger("devflow.config").info("config.yaml 已热加载：%d 个项目", len(self.projects))
        return True

    @property
    def base_dir(self) -> Path:
        return self._path.parent

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        if not p.is_absolute():
            p = self.base_dir / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "devflow.sqlite3"

    @property
    def dashboard_url(self) -> str:
        return f"http://{self.dashboard.host}:{self.dashboard.port}"

    def project(self, name: str) -> Optional[ProjectCfg]:
        for p in self.projects:
            if p.name == name:
                return p
        return None

    def is_own(self, name: str) -> bool:
        p = self.project(name)
        return bool(p and p.kind == "own")

    def group_members(self, group: str) -> list[ProjectCfg]:
        return [p for p in self.projects if p.group == group]

    def display_name(self, name: str) -> str:
        """面板显示名：分组仓库显示成「产品 › 前端」而不是 Morphra-frontend。"""
        p = self.project(name)
        if not p:
            return name
        if p.group:
            suffix = re.sub(rf"^{re.escape(p.group)}[-_ ]*", "", p.name, flags=re.I) or p.name
            return f"{p.group} › {suffix}"
        return p.name

    def choices(self) -> list[dict]:
        """面板下拉项：同一 group 的仓库合并成一项（value = group:名），其余按项目名。"""
        out: list[dict] = []
        seen: set[str] = set()
        for p in self.projects:
            if p.group:
                if p.group in seen:
                    continue
                seen.add(p.group)
                members = self.group_members(p.group)
                out.append({"value": f"group:{p.group}", "label": f"{p.group}（{len(members)} 个仓库）",
                            "kind": members[0].kind})
            else:
                out.append({"value": p.name, "label": p.name, "kind": p.kind})
        return out


def find_config_path(explicit: str | None = None) -> Path:
    candidates = [
        explicit,
        os.environ.get("DEVFLOW_CONFIG"),
        "config.yaml",
        str(Path(__file__).resolve().parent.parent / "config.yaml"),
        str(Path.home() / ".devflow" / "config.yaml"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return Path(c).resolve()
    raise FileNotFoundError("找不到 config.yaml。请先运行 `devflow init`，或用 --config 指定路径。")


def load_config(explicit: str | None = None) -> Config:
    path = find_config_path(explicit)
    load_dotenv(path.parent / ".env")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(data)
    cfg._path = path
    cfg._mtime = path.stat().st_mtime
    # 敏感信息允许用环境变量覆盖
    cfg.dingtalk.client_id = os.environ.get("DINGTALK_CLIENT_ID") or cfg.dingtalk.client_id
    cfg.dingtalk.client_secret = os.environ.get("DINGTALK_CLIENT_SECRET") or cfg.dingtalk.client_secret
    if not cfg.dingtalk.robot_code:
        cfg.dingtalk.robot_code = cfg.dingtalk.client_id
    cfg.auth.username = os.environ.get("DEVFLOW_USERNAME") or cfg.auth.username
    cfg.auth.password = os.environ.get("DEVFLOW_PASSWORD") or cfg.auth.password
    cfg.auth.secret = os.environ.get("DEVFLOW_SECRET") or cfg.auth.secret
    return cfg

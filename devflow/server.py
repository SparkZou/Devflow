"""本地面板（FastAPI）：粘贴需求、看进度、点确认。"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .auth import install_auth
from .config import Config
from .models import STATE_LABELS, needs_action
from .monitor import SiteMonitor
from .pipeline import Pipeline
from .store import Store

log = logging.getLogger("devflow.server")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}


class InboxIn(BaseModel):
    text: str = ""
    source: str = "manual"
    project: str = ""
    group: str = ""
    customer: str = ""
    attachments: list[str] = Field(default_factory=list)  # 本机图片路径


def _split_choice(value: str) -> tuple[str, str]:
    """下拉框的值：'group:Morphra' -> ("", "Morphra")；'JobAI' -> ("JobAI", "")。"""
    if value.startswith("group:"):
        return "", value[6:]
    return value, ""


def _back(request: Request) -> str:
    """当前页面地址（含 ?project=…），表单提交后跳回来。"""
    return request.url.path + (f"?{request.url.query}" if request.url.query else "")


def _safe_back(value: str) -> str:
    return value if value.startswith("/") and not value.startswith("//") else "/"


def create_app(cfg: Config, store: Store, pipeline: Pipeline, monitor: SiteMonitor | None = None) -> FastAPI:
    app = FastAPI(title="DevFlow AI", docs_url="/api/docs", redoc_url=None)
    monitor = monitor or SiteMonitor(cfg)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    install_auth(app, cfg, TEMPLATES)  # 除 /login /logout /health 外都要登录（或 HTTP Basic）

    def ctx(request: Request, **kw) -> dict:
        return {"request": request, "cfg": cfg, "labels": STATE_LABELS, "ai_backend": pipeline.ai.backend.name,
                "choices": cfg.choices(), "disp": cfg.display_name, "is_own": cfg.is_own,
                "auth_on": cfg.auth.enabled, "back": _back(request), **kw}

    def save_uploads(files: list[UploadFile]) -> list[str]:
        paths: list[str] = []
        folder = cfg.data_path / "attachments" / f"{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
        for f in files[:10]:
            if not f.filename or (f.content_type or "") not in IMAGE_TYPES:
                continue
            data = f.file.read()
            if not data:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            ext = Path(f.filename).suffix.lower() or ".png"
            p = folder / f"{len(paths) + 1}{ext}"
            p.write_bytes(data)
            paths.append(str(p))
        return paths

    # ------------------------------------------------------------ 左栏导航
    def task_keys(t) -> set[str]:
        """一条任务在左栏属于哪些条目：项目名、所属 group、客户/自有分类；没归属的记 none。"""
        p = cfg.project(t.project) if t.project else None
        if p:
            keys = {p.name, f"kind:{p.kind}"}
            if p.group:
                keys.add(f"group:{p.group}")
            return keys
        g = t.meta.get("group") if isinstance(t.meta, dict) else ""
        if g:
            members = cfg.group_members(g)
            return {f"group:{g}", f"kind:{members[0].kind if members else 'customer'}"}
        return {"none"}

    def selection(sel: str) -> tuple[str, list, list]:
        """选中的条目 → 页面标题、监控涉及的项目、顶部展示链接的项目。"""
        if not sel:
            return "全部项目", cfg.projects, []
        if sel == "kind:own":
            return "自己的项目", [p for p in cfg.projects if p.kind == "own"], []
        if sel == "kind:customer":
            return "客户项目", [p for p in cfg.projects if p.kind != "own"], []
        if sel == "none":
            return "未归类（等你选项目）", [], []
        if sel.startswith("group:"):
            members = cfg.group_members(sel[6:])
            return sel[6:], members, members
        p = cfg.project(sel)
        return cfg.display_name(sel), [p], [p]

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, sel: str = Query("", alias="project")):
        cfg.reload_if_changed()  # 改了 config.yaml 刷新面板即生效，不用重启
        choices = cfg.choices()
        known = {"", "kind:own", "kind:customer", "none", *(c["value"] for c in choices), *(p.name for p in cfg.projects)}
        if sel not in known:
            sel = ""
        tasks = store.list(limit=300)

        counts: dict[str, list[int]] = {}
        for t in tasks:
            if t.is_done:
                continue
            slot = 0 if needs_action(t, cfg.gates) else 1
            for k in task_keys(t) | {""}:
                counts.setdefault(k, [0, 0])[slot] += 1

        def cert_min(projs) -> int | None:
            days = [r["cert_days"] for p in projs if p and (r := monitor.for_project(p)) and r["cert_days"] is not None]
            return min(days) if days else None

        def item(value: str, label: str, projs: list) -> dict:
            c = counts.get(value, [0, 0])
            return {"value": value, "label": label, "action": c[0], "active": c[1], "selected": value == sel,
                    "cert_days": cert_min(projs)}

        groups = []
        for kind, title in (("customer", "客户项目"), ("own", "自己的项目")):
            items = []
            for c in choices:
                if (c["kind"] == "own") != (kind == "own"):
                    continue
                members = cfg.group_members(c["value"][6:]) if c["value"].startswith("group:") else [cfg.project(c["value"])]
                items.append(item(c["value"], c["label"], members))
            g = item(f"kind:{kind}", title, [p for p in cfg.projects if (p.kind == "own") == (kind == "own")])
            g.update(title=title, entries=items)
            groups.append(g)
        nav = {"all": item("", "全部", cfg.projects), "groups": groups, "unassigned": item("none", "未归类", [])}

        title, mon_projects, detail = selection(sel)
        visible = tasks if not sel else [t for t in tasks if sel in task_keys(t)]
        action = [t for t in visible if needs_action(t, cfg.gates)]
        action_ids = {t.id for t in action}
        active = [t for t in visible if not t.is_done and t.id not in action_ids]
        done = [t for t in visible if t.is_done][:30]
        soonest = min((row["result"] for row in monitor.rows() if row["result"] and row["result"]["cert_days"] is not None),
                      key=lambda r: r["cert_days"], default=None)
        return TEMPLATES.TemplateResponse(request, "dashboard.html", ctx(
            request, action=action, active=active, done=done, nav=nav, title=title, detail_projects=detail,
            monitor_rows=monitor.rows(mon_projects), refreshed_at=monitor.refreshed_at,
            monitor_ttl_min=max(1, monitor.ttl // 60), cert_soonest=soonest,
            preselect=sel if sel in {c["value"] for c in choices} else ""))

    @app.post("/monitor/refresh")
    def monitor_refresh(back: str = Form("/")):
        try:
            monitor.refresh()
        except Exception as e:  # noqa: BLE001
            log.warning("手动刷新监控失败: %s", e)
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.get("/task/{task_id}", response_class=HTMLResponse)
    def task_page(request: Request, task_id: int):
        cfg.reload_if_changed()
        task = store.get(task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        return TEMPLATES.TemplateResponse(
            request, "task.html", ctx(request, t=task, needs_action=needs_action(task, cfg.gates)))

    @app.get("/task/{task_id}/screenshot")
    def screenshot(task_id: int):
        task = store.get(task_id)
        if not task or not task.screenshot_path or not Path(task.screenshot_path).exists():
            raise HTTPException(404)
        return FileResponse(task.screenshot_path, media_type="image/png")

    @app.get("/task/{task_id}/attachment/{index}")
    def attachment(task_id: int, index: int):
        task = store.get(task_id)
        if not task or index < 0 or index >= len(task.attachments) or not Path(task.attachments[index]).exists():
            raise HTTPException(404)
        return FileResponse(task.attachments[index])

    # ------------------------------------------------------------ form actions
    @app.post("/inbox")
    def inbox_form(text: str = Form(""), project: str = Form(""), customer: str = Form(""),
                   source: str = Form("wechat"), files: list[UploadFile] = File(default=[]), back: str = Form("/")):
        paths = save_uploads(files)
        if text.strip() or paths:
            proj, group = _split_choice(project)
            pipeline.ingest(text, source=source, project=proj, group=group, customer=customer, attachments=paths)
        return RedirectResponse(_safe_back(back), status_code=303)

    @app.post("/task/{task_id}/{action}")
    def task_action(task_id: int, action: str, note: str = Form(""),
                    title: Optional[str] = Form(None), project: Optional[str] = Form(None),
                    customer: Optional[str] = Form(None), priority: Optional[str] = Form(None),
                    deadline: Optional[str] = Form(None), description: Optional[str] = Form(None),
                    reply_draft: Optional[str] = Form(None), back: str = Form("")):
        try:
            _do(action, task_id, note=note, title=title, project=project, customer=customer,
                priority=priority, deadline=deadline, description=description, reply_draft=reply_draft)
        except (ValueError, KeyError) as e:
            return HTMLResponse(f"<p style='font-family:sans-serif'>❌ {e}</p><a href='/task/{task_id}'>返回</a>",
                                status_code=400)
        return RedirectResponse(back or f"/task/{task_id}", status_code=303)

    def _do(action: str, task_id: int, note: str = "", **fields) -> str:
        if action == "approve":
            return pipeline.approve(task_id)
        if action == "retry":
            return pipeline.retry(task_id, note=note)
        if action == "retriage":
            return pipeline.retriage(task_id)
        if action == "resolve":
            return "resolved: " + ("、".join(pipeline.resolve(task_id, note=note)) or "无需清理")
        if action == "ignore":
            pipeline.ignore(task_id)
            return "ignored"
        if action == "update":
            pipeline.update(task_id, **fields)
            return "updated"
        if action == "save_and_approve":
            pipeline.update(task_id, **fields)
            return pipeline.approve(task_id)
        raise ValueError(f"未知操作 {action}")

    @app.get("/manifest.webmanifest")
    def manifest():
        """手机「添加到主屏幕」后以独立 App 形式打开。"""
        return JSONResponse({
            "name": "DevFlow AI", "short_name": "DevFlow", "start_url": "/", "display": "standalone",
            "background_color": "#f6f7f9", "theme_color": "#2563eb",
            "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
        }, media_type="application/manifest+json")

    # ------------------------------------------------------------ JSON API（CLI 用）
    @app.get("/health")
    def health():
        return {"ok": True, "ai": pipeline.ai.backend.name}

    @app.get("/api/tasks")
    def api_tasks(all: bool = False):
        tasks = store.list(limit=500)
        if not all:
            tasks = [t for t in tasks if not t.is_done]
        return [t.model_dump() for t in tasks]

    @app.get("/api/tasks/{task_id}")
    def api_task(task_id: int):
        task = store.get(task_id)
        if not task:
            raise HTTPException(404)
        return task.model_dump()

    @app.post("/api/inbox")
    def api_inbox(body: InboxIn):
        proj, group = _split_choice(body.project)
        task = pipeline.ingest(body.text, source=body.source, project=proj, group=body.group or group,
                               customer=body.customer, attachments=body.attachments)
        return task.model_dump()

    @app.post("/api/tasks/{task_id}/{action}")
    def api_action(task_id: int, action: str, body: dict | None = None):
        body = body or {}
        try:
            result = _do(action, task_id, note=body.pop("note", ""), **body)
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"ok": True, "action": result, "task": store.get(task_id).model_dump()}

    return app

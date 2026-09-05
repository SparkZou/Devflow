"""本地面板（FastAPI）：粘贴需求、看进度、点确认。"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .auth import install_auth
from .config import Config
from .models import STATE_LABELS, needs_action
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


def create_app(cfg: Config, store: Store, pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="DevFlow AI", docs_url="/api/docs", redoc_url=None)
    install_auth(app, cfg, TEMPLATES)  # 除 /login /logout /health 外都要登录（或 HTTP Basic）

    def ctx(request: Request, **kw) -> dict:
        return {"request": request, "cfg": cfg, "labels": STATE_LABELS, "ai_backend": pipeline.ai.backend.name,
                "choices": cfg.choices(), "disp": cfg.display_name, "is_own": cfg.is_own,
                "auth_on": cfg.auth.enabled, **kw}

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

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        cfg.reload_if_changed()  # 改了 config.yaml 刷新面板即生效，不用重启
        tasks = store.list(limit=300)
        action = [t for t in tasks if needs_action(t, cfg.gates)]
        action_ids = {t.id for t in action}
        active = [t for t in tasks if not t.is_done and t.id not in action_ids]
        done = [t for t in tasks if t.is_done][:30]
        return TEMPLATES.TemplateResponse(
            request, "dashboard.html", ctx(request, action=action, active=active, done=done))

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
                   source: str = Form("wechat"), files: list[UploadFile] = File(default=[])):
        paths = save_uploads(files)
        if text.strip() or paths:
            proj, group = _split_choice(project)
            pipeline.ingest(text, source=source, project=proj, group=group, customer=customer, attachments=paths)
        return RedirectResponse("/", status_code=303)

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

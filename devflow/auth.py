"""面板登录：config.yaml `auth` 段的用户名/密码 → 签名 Cookie 会话；/api/* 也接受 HTTP Basic（CLI 用）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Optional
from urllib.parse import quote

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import Config

log = logging.getLogger("devflow.auth")
COOKIE = "devflow_session"
OPEN_PATHS = {"/login", "/logout", "/health", "/favicon.ico"}
MAX_FAILS, LOCK_SECONDS = 5, 60  # 同一 IP 60 秒内错 5 次 → 锁 60 秒


class Sessions:
    """HMAC 签名的会话令牌 `<过期时间戳>.<签名>`。签名密钥混入用户名+密码：改密码后旧会话自动失效。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._fails: dict[str, list[float]] = {}

    def _file_secret(self) -> str:
        p = self.cfg.data_path / ".session_secret"
        try:
            s = p.read_text(encoding="utf-8").strip()
            if s:
                return s
        except OSError:
            pass
        s = secrets.token_hex(32)
        p.write_text(s, encoding="utf-8")
        return s

    def _key(self) -> bytes:
        a = self.cfg.auth
        return hashlib.sha256(f"{a.secret or self._file_secret()}|{a.username}|{a.password}".encode()).digest()

    def _sign(self, exp: int) -> str:
        return hmac.new(self._key(), str(exp).encode(), hashlib.sha256).hexdigest()

    def issue(self) -> str:
        exp = int(time.time()) + self.cfg.auth.session_days * 86400
        return f"{exp}.{self._sign(exp)}"

    def valid(self, token: Optional[str]) -> bool:
        if not token or "." not in token:
            return False
        exp_s, sig = token.split(".", 1)
        if not exp_s.isdigit() or int(exp_s) < time.time():
            return False
        return hmac.compare_digest(sig, self._sign(int(exp_s)))

    def check_password(self, username: str, password: str) -> bool:
        a = self.cfg.auth
        ok_user = hmac.compare_digest(username.encode(), a.username.encode())
        ok_pass = hmac.compare_digest(password.encode(), a.password.encode())
        return ok_user and ok_pass

    def basic_ok(self, header: Optional[str]) -> bool:
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            raw = base64.b64decode(header[6:].strip()).decode("utf-8")
        except Exception:  # noqa: BLE001
            return False
        user, _, pw = raw.partition(":")
        return self.check_password(user, pw)

    # ---- 防暴力
    def locked_for(self, ip: str) -> int:
        now = time.time()
        fails = [t for t in self._fails.get(ip, []) if now - t < LOCK_SECONDS]
        self._fails[ip] = fails
        return int(LOCK_SECONDS - (now - fails[0])) + 1 if len(fails) >= MAX_FAILS else 0

    def record_fail(self, ip: str) -> None:
        self._fails.setdefault(ip, []).append(time.time())

    def clear(self, ip: str) -> None:
        self._fails.pop(ip, None)


def _safe_next(value: str) -> str:
    """只允许站内相对路径，防止登录后跳到外站。"""
    return value if value.startswith("/") and not value.startswith("//") else "/"


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def install_auth(app: FastAPI, cfg: Config, templates: Jinja2Templates) -> Sessions:
    sessions = Sessions(cfg)

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if not cfg.auth.enabled or path in OPEN_PATHS:
            return await call_next(request)
        if sessions.valid(request.cookies.get(COOKIE)) or sessions.basic_ok(request.headers.get("authorization")):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized", "hint": "请用 HTTP Basic（config.yaml auth 段的用户名/密码）"},
                                status_code=401)
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=303)

    def page(request: Request, error: str = "", nxt: str = "/", username: str = "", status: int = 200):
        return templates.TemplateResponse(
            request, "login.html", {"error": error, "next": _safe_next(nxt), "username": username}, status_code=status)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, nxt: str = Query("/", alias="next")):
        if not cfg.auth.enabled or sessions.valid(request.cookies.get(COOKIE)):
            return RedirectResponse(_safe_next(nxt), status_code=303)
        return page(request, nxt=nxt)

    @app.post("/login", response_class=HTMLResponse)
    def login_submit(request: Request, username: str = Form(""), password: str = Form(""),
                     nxt: str = Form("/", alias="next")):
        ip = _ip(request)
        wait = sessions.locked_for(ip)
        if wait:
            return page(request, f"错误次数太多，请 {wait} 秒后再试", nxt, username, status=429)
        if not sessions.check_password(username.strip(), password):
            sessions.record_fail(ip)
            log.warning("登录失败 user=%s ip=%s", username, ip)
            return page(request, "用户名或密码不对", nxt, username, status=401)
        sessions.clear(ip)
        log.info("登录成功 user=%s ip=%s", username, ip)
        resp = RedirectResponse(_safe_next(nxt), status_code=303)
        resp.set_cookie(COOKIE, sessions.issue(), max_age=cfg.auth.session_days * 86400, httponly=True,
                        samesite="lax", secure=request.url.scheme == "https", path="/")
        return resp

    @app.get("/logout")
    def logout():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE, path="/")
        return resp

    return sessions

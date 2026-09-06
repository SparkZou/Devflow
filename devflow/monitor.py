"""站点监控：各项目线上地址的健康状态 + HTTPS 证书到期时间。后台线程定时刷新，面板读缓存。"""
from __future__ import annotations

import datetime as dt
import logging
import socket
import ssl
import threading
import time
from typing import Optional
from urllib.parse import urlparse

from .config import Config, ProjectCfg

log = logging.getLogger("devflow.monitor")
CERT_WARN_DAYS = 14  # 证书剩余天数低于这个值 → 面板标红、进日报


def parse_not_after(value: str) -> dt.datetime:
    """openssl 的 notAfter 格式：'Nov 13 09:12:00 2026 GMT'"""
    return dt.datetime.strptime(value, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)


def check_cert(host: str, timeout: float = 8.0) -> dict:
    """连一次 443 拿证书：到期时间、剩余天数、签发方。"""
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    expires = parse_not_after(cert["notAfter"])
    issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "")
    days = (expires - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
    return {"cert_expires": expires, "cert_days": int(days // 1), "cert_issuer": issuer}


def check_health(url: str, timeout: float = 8.0) -> dict:
    import httpx

    t0 = time.monotonic()
    r = httpx.get(url, timeout=timeout, follow_redirects=True, headers={"User-Agent": "DevFlow-AI monitor"})
    return {"status": r.status_code, "ms": int((time.monotonic() - t0) * 1000), "ok": r.status_code < 400}


def check_site(url: str, health_url: str = "") -> dict:
    """一个站点的完整检查；任何一项失败不影响另一项，错误写进 *_error。"""
    host = urlparse(url).hostname or url
    out: dict = {"url": url, "host": host, "checked_at": dt.datetime.now().replace(microsecond=0),
                 "ok": None, "status": None, "ms": None, "error": "",
                 "cert_expires": None, "cert_days": None, "cert_issuer": "", "cert_error": ""}
    if url.startswith("https://"):
        try:
            out.update(check_cert(host))
        except Exception as e:  # noqa: BLE001
            out["cert_error"] = str(e)[:120]
    else:
        out["cert_error"] = "非 https"
    try:
        out.update(check_health(health_url or url))
    except Exception as e:  # noqa: BLE001
        out["ok"], out["error"] = False, str(e)[:120]
    return out


class SiteMonitor:
    """按 deploy_url 的主机去重检查（前后端共用域名只查一次），结果缓存 ttl 秒。"""

    def __init__(self, cfg: Config, ttl: int = 600):
        self.cfg, self.ttl = cfg, ttl
        self._results: dict[str, dict] = {}  # url -> result
        self._lock = threading.Lock()
        self._refreshed_at: Optional[dt.datetime] = None

    # ---- 读
    @property
    def refreshed_at(self) -> Optional[dt.datetime]:
        return self._refreshed_at

    def for_project(self, proj: ProjectCfg) -> Optional[dict]:
        if not proj.deploy_url:
            return None
        with self._lock:
            return self._results.get(proj.deploy_url.rstrip("/"))

    def rows(self, projects: Optional[list[ProjectCfg]] = None) -> list[dict]:
        """面板表格：每个有 deploy_url 的项目一行。"""
        out = []
        for p in (projects if projects is not None else self.cfg.projects):
            if p.deploy_url:
                out.append({"project": p, "result": self.for_project(p)})
        return out

    def expiring(self, days: int = CERT_WARN_DAYS) -> list[tuple[ProjectCfg, dict]]:
        """证书剩余天数 ≤ days 的项目（日报用）。"""
        seen, out = set(), []
        for row in self.rows():
            r = row["result"]
            if r and r["cert_days"] is not None and r["cert_days"] <= days and r["host"] not in seen:
                seen.add(r["host"])
                out.append((row["project"], r))
        return out

    # ---- 写
    def refresh(self) -> int:
        self.cfg.reload_if_changed()
        urls = {p.deploy_url.rstrip("/"): p.health_url for p in self.cfg.projects if p.deploy_url}
        results = {}
        for url, health_url in urls.items():
            results[url] = check_site(url, health_url)
            r = results[url]
            log.info("监控 %s → %s %sms · 证书剩 %s 天 %s", r["host"], r["status"], r["ms"], r["cert_days"],
                     r["error"] or r["cert_error"])
        with self._lock:
            self._results = results
            self._refreshed_at = dt.datetime.now().replace(microsecond=0)
        return len(results)

    def start(self) -> None:
        def loop():
            while True:
                try:
                    self.refresh()
                except Exception as e:  # noqa: BLE001
                    log.warning("监控刷新失败: %s", e)
                time.sleep(self.ttl)

        threading.Thread(target=loop, name="site-monitor", daemon=True).start()

"""定时提醒：每天固定时间把"还有哪些没做 / 哪些等你确认"推给你。"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from .config import Config
from .models import needs_action
from .notify import Notifier
from .store import Store

log = logging.getLogger("devflow.scheduler")


def build_digest(store: Store, cfg: Config, monitor=None) -> tuple[str, str]:
    tasks = [t for t in store.list(limit=500) if not t.is_done]
    action = [t for t in tasks if needs_action(t, cfg.gates)]
    action_ids = {t.id for t in action}
    others = [t for t in tasks if t.id not in action_ids]
    overdue = [t for t in tasks if t.overdue]
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

    def line(t) -> str:
        s = f"- #{t.id} [{t.priority}] {t.title or t.raw_text[:20]} — {t.label}"
        if t.project:
            s += f"（{t.project}）"
        if t.deadline:
            s += f" ⏰{t.deadline}" + ("【逾期】" if t.overdue else "")
        return s

    lines = [f"未完成 {len(tasks)} 个 · 等你处理 {len(action)} 个 · 逾期 {len(overdue)} 个", ""]
    if action:
        lines.append("**等你处理：**")
        lines += [line(t) for t in sorted(action, key=lambda t: (order.get(t.priority, 9), t.deadline or "9"))]
    if others:
        lines += ["", "**自动进行中：**"]
        lines += [line(t) for t in sorted(others, key=lambda t: (order.get(t.priority, 9), t.deadline or "9"))]
    expiring = monitor.expiring() if monitor else []
    if expiring:
        lines += ["", "**HTTPS 证书快到期：**"]
        lines += [f"- 🔒 {r['host']} 证书 {r['cert_days']} 天后到期（{r['cert_expires']:%Y-%m-%d}，{p.name}）" for p, r in expiring]
    lines += ["", f"面板：{cfg.dashboard_url}"]
    title = f"DevFlow 日报 {dt.date.today().isoformat()}"
    return title, "\n".join(lines)


def start_scheduler(cfg: Config, store: Store, notifier: Notifier, monitor=None) -> threading.Thread:
    def _loop():
        log.info("提醒计划已启动：%s", ", ".join(cfg.digest.times))
        stop = threading.Event()
        while not stop.wait(30):
            now = dt.datetime.now()
            hhmm = now.strftime("%H:%M")
            if hhmm not in cfg.digest.times:
                continue
            key = f"{now.date()} {hhmm}"
            if store.kv_get("last_digest") == key:
                continue
            store.kv_set("last_digest", key)
            try:
                title, body = build_digest(store, cfg, monitor)
                notifier.me(title, body)
            except Exception:  # noqa: BLE001
                log.exception("发送日报失败")

    t = threading.Thread(target=_loop, name="scheduler", daemon=True)
    t.start()
    return t

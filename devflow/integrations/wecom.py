"""企业微信群机器人 webhook（只用于给自己发提醒；个人微信没有官方 API）。"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger("devflow.wecom")


def send_webhook_markdown(webhook: str, text: str) -> bool:
    if not webhook:
        return False
    try:
        r = httpx.post(webhook, json={"msgtype": "markdown", "markdown": {"content": text[:4000]}}, timeout=15)
        ok = r.status_code == 200 and r.json().get("errcode", 0) == 0
        if not ok:
            log.warning("企业微信 webhook 发送失败: %s", r.text[:300])
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("企业微信 webhook 异常: %s", e)
        return False

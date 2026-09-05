"""钉钉消息发送：自定义机器人 webhook（提醒自己）+ 企业内部应用机器人 OpenAPI（回复客户会话）。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse

import httpx

log = logging.getLogger("devflow.dingtalk")
API = "https://api.dingtalk.com"


def _signed_webhook(webhook: str, secret: str) -> str:
    if not secret:
        return webhook
    ts = str(round(time.time() * 1000))
    string_to_sign = f"{ts}\n{secret}".encode("utf-8")
    sign = base64.b64encode(hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest())
    return f"{webhook}&timestamp={ts}&sign={urllib.parse.quote_plus(sign)}"


def send_webhook_markdown(webhook: str, secret: str, title: str, text: str) -> bool:
    """自定义机器人（群机器人）webhook 发 markdown。"""
    if not webhook:
        return False
    try:
        r = httpx.post(
            _signed_webhook(webhook, secret),
            json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
            timeout=15,
        )
        ok = r.status_code == 200 and r.json().get("errcode", 0) == 0
        if not ok:
            log.warning("钉钉 webhook 发送失败: %s", r.text[:300])
        return ok
    except Exception as e:  # noqa: BLE001
        log.warning("钉钉 webhook 异常: %s", e)
        return False


class DingTalkAPI:
    """企业内部应用机器人主动发消息（需要应用开通"机器人发送消息"权限）。"""

    def __init__(self, client_id: str, client_secret: str, robot_code: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.robot_code = robot_code or client_id
        self._token = ""
        self._token_exp = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = httpx.post(
            f"{API}/v1.0/oauth2/accessToken",
            json={"appKey": self.client_id, "appSecret": self.client_secret},
            timeout=15,
        )
        r.raise_for_status()
        d = r.json()
        self._token = d["accessToken"]
        self._token_exp = time.time() + int(d.get("expireIn", 7200))
        return self._token

    def _post(self, path: str, body: dict) -> dict:
        r = httpx.post(
            f"{API}{path}", json=body,
            headers={"x-acs-dingtalk-access-token": self.access_token()}, timeout=20,
        )
        if r.status_code >= 300:
            raise RuntimeError(f"钉钉 API {path} 失败 {r.status_code}: {r.text[:300]}")
        return r.json()

    def send_markdown_to_users(self, user_ids: list[str], title: str, text: str) -> dict:
        return self._post("/v1.0/robot/oToMessages/batchSend", {
            "robotCode": self.robot_code,
            "userIds": user_ids,
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
        })

    def send_markdown_to_group(self, open_conversation_id: str, title: str, text: str) -> dict:
        return self._post("/v1.0/robot/groupMessages/send", {
            "robotCode": self.robot_code,
            "openConversationId": open_conversation_id,
            "msgKey": "sampleMarkdown",
            "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
        })

    def reply(self, meta: dict, title: str, text: str) -> bool:
        """给某次来消息的会话回复。优先用 sessionWebhook（不需要额外权限），过期后走 OpenAPI。"""
        webhook = meta.get("session_webhook")
        exp = float(meta.get("session_webhook_expired_time") or 0) / 1000
        if webhook and (not exp or time.time() < exp - 30):
            try:
                r = httpx.post(
                    webhook,
                    json={"msgtype": "markdown", "markdown": {"title": title, "text": text}},
                    timeout=15,
                )
                if r.status_code == 200 and r.json().get("errcode", 0) == 0:
                    return True
                log.warning("sessionWebhook 回复失败: %s", r.text[:200])
            except Exception as e:  # noqa: BLE001
                log.warning("sessionWebhook 异常: %s", e)
        if not self.configured:
            return False
        if str(meta.get("conversation_type")) == "2" and meta.get("conversation_id"):
            self.send_markdown_to_group(meta["conversation_id"], title, text)
            return True
        uid = meta.get("sender_staff_id")
        if uid:
            self.send_markdown_to_users([uid], title, text)
            return True
        return False

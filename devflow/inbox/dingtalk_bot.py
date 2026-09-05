"""钉钉 Stream 模式机器人：不需要公网 IP，本机直接长连接收消息。支持文字、图片、图文消息。"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import uuid
from pathlib import Path
from typing import Callable

import httpx

log = logging.getLogger("devflow.dingtalk_bot")


def start_dingtalk_bot(client_id: str, client_secret: str,
                       on_message: Callable[[str, dict], str],
                       attach_dir: str | Path | None = None) -> threading.Thread:
    import dingtalk_stream
    from dingtalk_stream import AckMessage

    attach_root = Path(attach_dir) if attach_dir else Path("data") / "attachments"

    class Handler(dingtalk_stream.ChatbotHandler):
        def __init__(self):
            super().__init__()
            self.logger = log

        def _download_images(self, msg) -> list[str]:
            paths: list[str] = []
            for i, code in enumerate((msg.get_image_list() or [])[:6]):
                try:
                    url = self.get_image_download_url(code)
                    if not url:
                        continue
                    r = httpx.get(url, timeout=60, follow_redirects=True)
                    r.raise_for_status()
                    ctype = r.headers.get("content-type", "")
                    ext = ".jpg" if "jpeg" in ctype or "jpg" in ctype else ".png"
                    attach_root.mkdir(parents=True, exist_ok=True)
                    p = attach_root / f"dt-{dt.datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}-{i}{ext}"
                    p.write_bytes(r.content)
                    paths.append(str(p))
                except Exception as e:  # noqa: BLE001
                    log.warning("下载钉钉图片失败: %s", e)
            return paths

        async def process(self, callback: dingtalk_stream.CallbackMessage):
            msg = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            texts = [t for t in (msg.get_text_list() or []) if t and t.strip()]
            text = "\n".join(texts).strip()
            loop = asyncio.get_running_loop()
            images = await loop.run_in_executor(None, self._download_images, msg)
            meta = {
                "conversation_type": msg.conversation_type,  # '1' 单聊 '2' 群聊
                "conversation_id": msg.conversation_id,
                "conversation_title": msg.conversation_title,
                "sender_nick": msg.sender_nick,
                "sender_staff_id": msg.sender_staff_id,
                "sender_id": msg.sender_id,
                "session_webhook": msg.session_webhook,
                "session_webhook_expired_time": msg.session_webhook_expired_time,
                "msg_id": msg.message_id,
                "attachments": images,
            }
            if not text and not images:
                self.reply_text("目前只支持文字和图片消息，请把需求用文字/截图发给我。", msg)
                return AckMessage.STATUS_OK, "OK"
            try:
                reply = await loop.run_in_executor(None, on_message, text, meta)
            except Exception as e:  # noqa: BLE001
                log.exception("处理钉钉消息失败")
                reply = f"处理失败：{e}"
            if reply:
                self.reply_markdown("DevFlow", reply, msg)
            return AckMessage.STATUS_OK, "OK"

    def _run():
        asyncio.set_event_loop(asyncio.new_event_loop())
        credential = dingtalk_stream.Credential(client_id, client_secret)
        client = dingtalk_stream.DingTalkStreamClient(credential)
        client.register_callback_handler(dingtalk_stream.chatbot.ChatbotMessage.TOPIC, Handler())
        log.info("钉钉机器人已启动（Stream 模式，长连接）")
        client.start_forever()

    t = threading.Thread(target=_run, name="dingtalk-bot", daemon=True)
    t.start()
    return t

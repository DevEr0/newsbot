"""Minimal Telegram Bot API client (HTML messages, photos, inline buttons)."""
import json
import logging
import time

import requests

log = logging.getLogger("tg")


class Telegram:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.base = f"https://api.telegram.org/bot{token}"
        self.s = requests.Session()

    def _safe(self, text):
        # request errors embed the URL, i.e. the token. Never let it reach logs.
        return str(text).replace(self.token, "<TOKEN>")

    def _call(self, method, data=None, files=None, timeout=40, retries=3):
        for _ in range(retries):
            try:
                r = self.s.post(f"{self.base}/{method}", data=data, files=files, timeout=timeout)
                j = r.json()
            except Exception as e:  # network hiccup / bad JSON
                log.warning("telegram %s error: %s", method, self._safe(e))
                time.sleep(2)
                continue
            if j.get("ok"):
                return j["result"]
            if r.status_code == 429:
                time.sleep(j.get("parameters", {}).get("retry_after", 3) + 1)
                continue
            log.error("telegram %s failed: %s", method, j.get("description"))
            return None
        return None

    def send_message(self, text, markup=None, preview=False, chat_id=None):
        data = {"chat_id": chat_id or self.chat_id, "text": text[:4090], "parse_mode": "HTML",
                "disable_web_page_preview": "false" if preview else "true"}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        return self._call("sendMessage", data)

    def send_photo(self, png, caption="", markup=None, chat_id=None):
        data = {"chat_id": chat_id or self.chat_id, "caption": caption[:1020], "parse_mode": "HTML"}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        return self._call("sendPhoto", data, files={"photo": ("chart.png", png, "image/png")})

    def edit_photo(self, message_id, png, caption="", markup=None, chat_id=None):
        media = {"type": "photo", "media": "attach://photo", "caption": caption[:1020], "parse_mode": "HTML"}
        data = {"chat_id": chat_id or self.chat_id, "message_id": message_id, "media": json.dumps(media)}
        if markup:
            data["reply_markup"] = json.dumps(markup)
        return self._call("editMessageMedia", data, files={"photo": ("chart.png", png, "image/png")})

    def answer_callback(self, cb_id, text=""):
        return self._call("answerCallbackQuery", {"callback_query_id": cb_id, "text": text}, retries=1)

    def get_updates(self, offset, timeout=0):
        data = {"offset": offset, "timeout": timeout,
                "allowed_updates": json.dumps(["message", "callback_query"])}
        return self._call("getUpdates", data, timeout=timeout + 15) or []

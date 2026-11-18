"""Minimal Telegram Bot API client: sends drafts with buttons and reads your answers."""
import requests

TIMEOUT = 30


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id)

    def _call(self, method: str, **params):
        r = requests.post(f"{self.base}/{method}", json=params, timeout=TIMEOUT)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def get_me(self):
        return self._call("getMe")

    def send(self, text: str, buttons=None, html: bool = True):
        params = {"chat_id": self.chat_id, "text": text[:4096], "disable_web_page_preview": True}
        if html:
            params["parse_mode"] = "HTML"
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", **params)

    def send_photo(self, photo_url: str, caption: str = ""):
        return self._call("sendPhoto", chat_id=self.chat_id, photo=photo_url,
                          caption=caption[:1024], parse_mode="HTML")

    def send_album(self, photo_urls: list[str], caption: str = ""):
        media = [{"type": "photo", "media": u} for u in photo_urls[:10]]
        if media:
            media[0]["caption"] = caption[:1024]
            media[0]["parse_mode"] = "HTML"
        return self._call("sendMediaGroup", chat_id=self.chat_id, media=media)

    def send_photo_file(self, path, caption: str = ""):
        """Upload the image itself (more reliable than asking Telegram to fetch a link)."""
        with open(path, "rb") as f:
            r = requests.post(f"{self.base}/sendPhoto", timeout=60,
                              data={"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML"},
                              files={"photo": f})
        return self._check("sendPhoto", r)

    def send_video_file(self, path, caption: str = ""):
        with open(path, "rb") as f:
            r = requests.post(f"{self.base}/sendVideo", timeout=120,
                              data={"chat_id": self.chat_id, "caption": caption[:1024], "parse_mode": "HTML",
                                    "supports_streaming": "true", "width": "1080", "height": "1920"},
                              files={"video": f})
        return self._check("sendVideo", r)

    def send_album_files(self, paths, caption: str = ""):
        import json
        files, media = {}, []
        for i, path in enumerate(list(paths)[:10]):
            files[f"p{i}"] = open(path, "rb")
            item = {"type": "photo", "media": f"attach://p{i}"}
            if i == 0:
                item.update(caption=caption[:1024], parse_mode="HTML")
            media.append(item)
        try:
            r = requests.post(f"{self.base}/sendMediaGroup", timeout=90,
                              data={"chat_id": self.chat_id, "media": json.dumps(media)}, files=files)
        finally:
            for f in files.values():
                f.close()
        return self._check("sendMediaGroup", r)

    @staticmethod
    def _check(method, r):
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data["result"]

    def edit_text(self, message_id: int, text: str):
        try:
            self._call("editMessageText", chat_id=self.chat_id, message_id=message_id,
                       text=text[:4096], parse_mode="HTML", disable_web_page_preview=True)
        except RuntimeError:
            pass  # message too old / unchanged: not important

    def edit_buttons(self, message_id: int, buttons):
        try:
            self._call("editMessageReplyMarkup", chat_id=self.chat_id, message_id=message_id,
                       reply_markup={"inline_keyboard": buttons})
        except RuntimeError:
            pass

    def answer_callback(self, callback_id: str, text: str = ""):
        try:
            self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
        except RuntimeError:
            pass  # button presses older than a few minutes can't be answered; harmless

    def updates(self, offset: int):
        return self._call("getUpdates", offset=offset, timeout=0,
                          allowed_updates=["message", "callback_query"])


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

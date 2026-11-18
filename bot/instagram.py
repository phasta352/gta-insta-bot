"""Publishes posts through Meta's official Instagram Graph API (Facebook Login / system user)."""
import time

import requests

TIMEOUT = 60


class InstagramError(RuntimeError):
    pass


class Instagram:
    def __init__(self, ig_user_id: str, token: str, api_version: str = "v25.0"):
        self.ig = ig_user_id
        self.token = token
        self.base = f"https://graph.facebook.com/{api_version}"

    def _req(self, method: str, path: str, **params):
        params["access_token"] = self.token
        url = f"{self.base}/{path}"
        r = (requests.get(url, params=params, timeout=TIMEOUT) if method == "GET"
             else requests.post(url, data=params, timeout=TIMEOUT))
        data = r.json()
        if "error" in data:
            e = data["error"]
            raise InstagramError(f"{e.get('message')} (code {e.get('code')}, subcode {e.get('error_subcode')})")
        return data

    def account(self) -> dict:
        return self._req("GET", self.ig, fields="username,name,followers_count")

    def quota(self) -> dict:
        data = self._req("GET", f"{self.ig}/content_publishing_limit", fields="quota_usage,config")
        return (data.get("data") or [{}])[0]

    def publish_image(self, image_url: str, caption: str, alt_text: str = "") -> str:
        params = {"image_url": image_url, "caption": caption[:2200]}
        if alt_text:
            params["alt_text"] = alt_text[:1000]
        try:
            container = self._req("POST", f"{self.ig}/media", **params)
        except InstagramError as ex:
            if alt_text and "alt_text" in str(ex):
                params.pop("alt_text")
                container = self._req("POST", f"{self.ig}/media", **params)
            else:
                raise
        cid = container["id"]

        # Wait until Instagram has fetched and processed the image.
        for _ in range(20):
            status = self._req("GET", cid, fields="status_code").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Media container {status}")
            time.sleep(3)

        result = self._req("POST", f"{self.ig}/media_publish", creation_id=cid)
        return result["id"]

    def _wait(self, container_id: str):
        for _ in range(30):
            status = self._req("GET", container_id, fields="status_code").get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise InstagramError(f"Media container {status}")
            time.sleep(3)

    def publish_carousel(self, image_urls: list[str], caption: str, alt_text: str = "") -> str:
        """2-10 images in one post. Each image becomes a carousel item, then one parent post."""
        children = []
        for i, url in enumerate(image_urls[:10]):
            params = {"image_url": url, "is_carousel_item": "true"}
            if i == 0 and alt_text:
                params["alt_text"] = alt_text[:1000]
            try:
                item = self._req("POST", f"{self.ig}/media", **params)
            except InstagramError as ex:
                if "alt_text" in params and "alt_text" in str(ex):
                    params.pop("alt_text")
                    item = self._req("POST", f"{self.ig}/media", **params)
                else:
                    raise
            children.append(item["id"])
        for cid in children:
            self._wait(cid)
        parent = self._req("POST", f"{self.ig}/media", media_type="CAROUSEL",
                           children=",".join(children), caption=caption[:2200])
        self._wait(parent["id"])
        return self._req("POST", f"{self.ig}/media_publish", creation_id=parent["id"])["id"]

    def publish_reel(self, video_url: str, caption: str) -> str:
        """Publish a vertical video as a Reel (also shown in the main feed)."""
        container = self._req("POST", f"{self.ig}/media", media_type="REELS", video_url=video_url,
                              caption=caption[:2200], share_to_feed="true", thumb_offset="1000")
        cid = container["id"]
        # Videos take longer to process than images: wait up to ~5 minutes.
        for _ in range(60):
            status = self._req("GET", cid, fields="status_code,status").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                detail = self._req("GET", cid, fields="status").get("status", "")
                raise InstagramError(f"Reel processing {status}: {detail}")
            time.sleep(5)
        else:
            raise InstagramError("Reel still processing after 5 minutes")
        return self._req("POST", f"{self.ig}/media_publish", creation_id=cid)["id"]

    def publish(self, image_urls: list[str], caption: str, alt_text: str = "") -> str:
        if len(image_urls) >= 2:
            return self.publish_carousel(image_urls, caption, alt_text)
        return self.publish_image(image_urls[0], caption, alt_text)

    def recent_media(self, limit: int = 10) -> list[dict]:
        """The account's latest posts with caption and type (used to avoid posting something twice)."""
        data = self._req("GET", f"{self.ig}/media", fields="caption,media_type,media_product_type", limit=str(limit))
        return data.get("data", [])

    def permalink(self, media_id: str) -> str:
        try:
            return self._req("GET", media_id, fields="permalink").get("permalink", "")
        except InstagramError:
            return ""

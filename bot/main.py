"""Entry point. GitHub Actions runs `python -m bot.main run` every 30 minutes.

Each run:
  1. reads your Telegram button presses and commands,
  2. publishes the next approved post if a posting slot is free,
  3. every few hours, looks for new stories and sends you drafts.
"""
import datetime as dt
import hashlib
import random
import sys
import time
import traceback
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import design, reel, sources, state as state_mod, storage
from .config import ROOT, load_secrets, load_settings
from .instagram import Instagram
from .telegram import Telegram, esc

OUT = ROOT / "out"

HELP = (
    "<b>Commands</b>\n"
    "/status: queue, drafts and today's numbers\n"
    "/scout: look for news on the next run\n"
    "/pause: stop scouting and posting\n"
    "/resume: start again\n\n"
    "Button presses are picked up on the next run (within about 30 minutes)."
)


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def log(msg: str) -> None:
    print(f"[bot] {msg}", flush=True)


class Bot:
    def __init__(self):
        self.settings = load_settings()
        self.secrets = load_secrets()
        self.state = state_mod.load()
        self.tz = ZoneInfo(self.settings["schedule"]["timezone"])
        self.tg = Telegram(self.secrets.telegram_token, self.secrets.telegram_chat_id)
        self.ig = Instagram(self.secrets.ig_user_id, self.secrets.ig_token,
                            self.settings["instagram"].get("api_version", "v25.0"))
        storage.configure(self.secrets, self.settings)
        self._bg_urls = None
        self._last_bg_url = None

    # ---------- helpers ----------
    def now_local(self):
        return dt.datetime.now(self.tz)

    def in_posting_hours(self) -> bool:
        start, end = self.settings["schedule"]["posting_hours"]
        return start <= self.now_local().hour <= end

    def posts_today(self) -> int:
        today = self.now_local().date()
        return sum(1 for p in self.state["published"]
                   if dt.datetime.fromtimestamp(p["at"], self.tz).date() == today)

    def drafts_today(self) -> int:
        """Drafts sent since midnight local time (the daily cap resets at midnight)."""
        today = self.now_local().date()
        return sum(1 for t in self.state["drafts_sent"]
                   if dt.datetime.fromtimestamp(t, self.tz).date() == today)

    def handle(self) -> str:
        h = self.settings["page"].get("handle") or self.state.get("ig_handle")
        if not h:
            try:
                h = "@" + self.ig.account()["username"]
                self.state["ig_handle"] = h
            except Exception:
                h = ""
        return h

    # ---------- 1. Telegram ----------
    def process_telegram(self):
        updates = self.tg.updates(self.state["telegram_offset"])
        log(f"Telegram: {len(updates)} new update(s) since offset {self.state['telegram_offset']}")
        for u in updates:
            self.state["telegram_offset"] = u["update_id"] + 1
            if "callback_query" in u:
                self.on_button(u["callback_query"])
            elif "message" in u:
                msg = u["message"]
                if str(msg.get("chat", {}).get("id")) != self.tg.chat_id:
                    continue  # ignore strangers
                self.on_command((msg.get("text") or "").strip().split()[0:1])

    def on_button(self, cb):
        if str(cb.get("message", {}).get("chat", {}).get("id")) != self.tg.chat_id:
            return
        action, _, draft_id = (cb.get("data") or "").partition(":")
        draft = (self.state["drafts"].get(draft_id) if action == "r"
                 else self.state["drafts"].pop(draft_id, None))
        log(f"Button '{action}' for draft {draft_id}: {'found' if draft else 'NOT FOUND in memory'}"
            f" (drafts in memory: {list(self.state['drafts'])})")
        if not draft:
            self.tg.answer_callback(cb["id"], "This draft is no longer available.")
            return
        mid = draft.get("text_message_id")
        if action == "r":
            self.make_reel_on_request(draft, cb["id"])
        elif action == "s":
            self.tg.answer_callback(cb["id"], "Skipped")
            if mid:
                self.tg.edit_text(mid, f"❌ <b>Skipped</b>: {esc(draft['headline'])}")
        elif action in ("a", "n", "c"):
            with_reel = action != "c" and bool(draft.get("reel_url"))
            self.queue_post(draft, post_now=action == "n", with_reel=with_reel)
            label = "Posting now" if action == "n" else "Approved, queued for the next slot"
            if with_reel:
                label += " (Reel follows at the next free slot)"
            self.tg.answer_callback(cb["id"], label)
            if mid:
                self.tg.edit_text(mid, f"✅ <b>{label}</b>: {esc(draft['headline'])}")

    @staticmethod
    def draft_buttons(draft):
        did = draft["id"]
        if draft.get("reel_url"):
            return [[{"text": "✅ Approve both", "callback_data": f"a:{did}"},
                     {"text": "⚡ Post now", "callback_data": f"n:{did}"}],
                    [{"text": "🖼 Carousel only", "callback_data": f"c:{did}"},
                     {"text": "❌ Skip", "callback_data": f"s:{did}"}]]
        rows = [[{"text": "✅ Approve", "callback_data": f"a:{did}"},
                 {"text": "⚡ Post now", "callback_data": f"n:{did}"}]]
        if "slides" in draft:   # drafts made before this feature can't be turned into a Reel
            rows.append([{"text": "🎬 Make Reel", "callback_data": f"r:{did}"},
                         {"text": "❌ Skip", "callback_data": f"s:{did}"}])
        else:
            rows.append([{"text": "❌ Skip", "callback_data": f"s:{did}"}])
        return rows

    def make_reel_on_request(self, draft, cb_id):
        """'Make Reel' button: build the Reel for an existing draft and update its buttons."""
        mid = draft.get("text_message_id")
        if draft.get("reel_url"):
            self.tg.answer_callback(cb_id, "This draft already has a Reel.")
            return
        self.tg.answer_callback(cb_id, "Making the Reel, about a minute…")
        try:
            background = self.download_background(draft["background_url"]) if draft.get("background_url") else None
        except Exception as ex:
            log(f"  -> background unavailable, using plain design: {ex}")
            background = None
        try:
            log(f"Making Reel on request: {draft['headline']}")
            path, url = self.build_reel(draft["id"], draft["headline"], draft.get("category", "NEWS"),
                                        draft["source"], draft.get("slides") or [], background)
        except Exception as ex:
            log(f"  -> Reel failed: {ex}")
            self.tg.send(f"🎬 Couldn't make the Reel for <b>{esc(draft['headline'])}</b>:\n"
                         f"<code>{esc(str(ex)[:300])}</code>")
            return
        draft["reel_url"] = url
        self.tg.send_video_file(path, f"🎬 <b>Reel ready</b>: {esc(draft['headline'])}\n"
                                      "Use the buttons on the draft to approve it.")
        if mid:
            self.tg.edit_buttons(mid, self.draft_buttons(draft))

    def queue_post(self, draft, post_now: bool, with_reel: bool):
        """Queue the carousel, and optionally the Reel as a separate post at a later slot."""
        carousel = {**draft, "kind": "carousel", "post_now": post_now, "approved_at": time.time()}
        if post_now:
            self.state["queue"].insert(0, carousel)
        else:
            self.state["queue"].append(carousel)
        if with_reel and draft.get("reel_url"):
            self.state["queue"].append({**draft, "kind": "reel", "post_now": False, "approved_at": time.time()})

    def on_command(self, parts):
        cmd = parts[0].lower().split("@")[0] if parts else ""
        log(f"Command: {cmd or '(plain message)'}")
        if cmd == "/pause":
            self.state["paused"] = True
            self.tg.send("⏸ Paused. Send /resume to start again.")
        elif cmd == "/resume":
            self.state["paused"] = False
            self.tg.send("▶️ Resumed.")
        elif cmd == "/scout":
            self.state["force_scout"] = True
            self.tg.send("🔎 I'll look for news on this run.")
        elif cmd == "/status":
            self.send_status()
        elif cmd in ("/start", "/help"):
            self.tg.send(HELP)

    def send_status(self):
        s = self.state
        sch = self.settings["schedule"]
        queued_reels = sum(1 for q in s["queue"] if q.get("kind") == "reel")
        queued_posts = len(s["queue"]) - queued_reels
        today = self.now_local().date()
        today_items = [p for p in s["published"] if dt.datetime.fromtimestamp(p["at"], self.tz).date() == today]
        reels_today = sum(1 for p in today_items if p.get("kind") == "reel")
        drafts_with_reel = sum(1 for d in s["drafts"].values() if d.get("reel_url"))
        lines = [
            f"<b>Status</b> ({'paused' if s['paused'] else 'running'})",
            f"Posted today: {len(today_items)} / {sch['max_posts_per_day']} "
            f"({_n(len(today_items) - reels_today, 'post')}, {_n(reels_today, 'Reel')})",
            f"Waiting to be posted: {_n(queued_posts, 'post')}, {_n(queued_reels, 'Reel')}",
        ]
        if s["queue"]:
            lines.append(f"Next free slot: {self.next_slot()}")
        lines.append(f"Drafts waiting for you: {len(s['drafts'])}"
                     + (f" ({drafts_with_reel} with a Reel)" if drafts_with_reel else ""))
        if s["last_scout"]:
            last = dt.datetime.fromtimestamp(s["last_scout"], self.tz).strftime("%H:%M")
            lines.append(f"Last news check: {last}")
        if s["feed_errors"]:
            lines.append("\n⚠️ Sources that failed last time:")
            lines += [f"• {esc(k)}: {esc(v[:80])}" for k, v in s["feed_errors"].items()]
        self.tg.send("\n".join(lines))

    def next_slot(self) -> str:
        """When the next queued item can go out, given the posting hours, spacing and daily limit."""
        if self.state["queue"] and self.state["queue"][0].get("post_now"):
            return "on the next run (Post now)"
        sch = self.settings["schedule"]
        start, end = sch["posting_hours"]
        last = max((p["at"] for p in self.state["published"]), default=0)
        t = max(self.now_local(), dt.datetime.fromtimestamp(last + sch["min_hours_between_posts"] * 3600, self.tz))
        if self.posts_today() >= sch["max_posts_per_day"] or t.hour > end:
            t = (t + dt.timedelta(days=1)).replace(hour=start, minute=0)
        elif t.hour < start:
            t = t.replace(hour=start, minute=0)
        day = "today" if t.date() == self.now_local().date() else "tomorrow"
        return f"{day} from {t.strftime('%H:%M')}"

    # ---------- 2. Publishing ----------
    def expire_drafts(self):
        limit = self.settings["schedule"]["draft_expiry_hours"] * 3600
        for did, d in list(self.state["drafts"].items()):
            if time.time() - d["created"] > limit:
                self.state["drafts"].pop(did)
                if d.get("text_message_id"):
                    self.tg.edit_text(d["text_message_id"], f"⌛ <b>Expired</b>: {esc(d['headline'])}")

    def already_on_instagram(self, draft, kind) -> bool:
        try:
            recent = self.ig.recent_media(10)
        except Exception as ex:
            log(f"Couldn't check recent posts ({ex}); continuing")
            return False
        want = " ".join(draft["full_caption"].split())[:120]
        for m in recent:
            is_reel = m.get("media_product_type") == "REELS"
            if is_reel != (kind == "reel"):
                continue
            if " ".join((m.get("caption") or "").split())[:120] == want:
                return True
        return False

    def publish_next(self):
        q = self.state["queue"]
        log(f"Queue: {len(q)} approved, paused={self.state['paused']}, "
            f"posts today={self.posts_today()}, posting hours={self.in_posting_hours()}")
        if not q or self.state["paused"]:
            return
        sch = self.settings["schedule"]
        draft = q[0]
        if not draft.get("post_now"):
            if not self.in_posting_hours() or self.posts_today() >= sch["max_posts_per_day"]:
                log("Not posting: outside posting hours or daily limit reached")
                return
            last = max((p["at"] for p in self.state["published"]), default=0)
            if time.time() - last < sch["min_hours_between_posts"] * 3600:
                log("Not posting: too soon after the last post")
                return
        kind = draft.get("kind", "carousel")
        # Guard 1: the bot's own memory says this exact post already went out.
        if any(p["id"] == draft["id"] and p.get("kind", "carousel") == kind for p in self.state["published"]):
            log(f"Skipping duplicate {kind} (already in memory): {draft['headline']}")
            q.pop(0)
            return
        # Guard 2: the same caption is already among the account's latest posts (e.g. memory was lost).
        # A Reel shares its caption with its carousel, so only posts of the same kind count.
        if self.already_on_instagram(draft, kind):
            log(f"Skipping duplicate {kind} (already on Instagram): {draft['headline']}")
            q.pop(0)
            self.state["published"].append({"id": draft["id"], "headline": draft["headline"], "kind": kind,
                                            "url": draft["url"], "at": time.time(), "media_id": "found-on-instagram"})
            return
        log(f"Publishing {kind}: {draft['headline']}")
        try:
            if kind == "reel":
                media_id = self.ig.publish_reel(draft["reel_url"], draft["full_caption"])
            else:
                urls = draft.get("image_urls") or [draft["image_url"]]
                media_id = self.ig.publish(urls, draft["full_caption"], draft.get("alt_text", ""))
        except Exception as ex:
            log(f"Publishing failed: {ex}")
            draft["attempts"] = draft.get("attempts", 0) + 1
            if draft["attempts"] >= 3:
                q.pop(0)
                self.tg.send(f"🚫 Gave up posting the {kind} after 3 tries: {esc(draft['headline'])}\n<code>{esc(str(ex))}</code>")
            else:
                self.tg.send(f"⚠️ Posting failed (try {draft['attempts']}/3), will retry next run:\n<code>{esc(str(ex))}</code>")
            return
        log(f"Published, media id {media_id}")
        q.pop(0)
        self.state["published"].append({"id": draft["id"], "headline": draft["headline"], "kind": kind,
                                        "url": draft["url"], "at": time.time(), "media_id": media_id})
        link = self.ig.permalink(media_id)
        what = "Reel" if kind == "reel" else "Posted"
        self.tg.send(f"📣 <b>{what}</b>: {esc(draft['headline'])}" + (f"\n{link}" if link else ""))

    # ---------- 3. Scouting ----------
    def should_scout(self) -> bool:
        if self.state["paused"]:
            return False
        if self.state.get("force_scout"):
            return True
        sch = self.settings["schedule"]
        start, end = sch["posting_hours"]
        if not (start - 1 <= self.now_local().hour <= end - 1):
            log(f"Not scouting: outside scouting hours ({start - 1}:00-{end - 1}:59 local)")
            return False
        if time.time() - self.state["last_scout"] < sch["scout_every_hours"] * 3600 - 300:
            log("Not scouting: last news check was less than "
                f"{sch['scout_every_hours']} h ago")
            return False
        if self.drafts_today() >= sch["max_drafts_per_day"]:
            log(f"Not scouting: daily draft limit reached ({sch['max_drafts_per_day']})")
            return False
        return True

    def scout(self):
        from .editor import Editor  # imported here so status-only runs stay fast

        forced = bool(self.state.get("force_scout"))   # manual scout: always report back
        self.state["force_scout"] = False
        self.state["last_scout"] = time.time()
        sch = self.settings["schedule"]
        candidates = sources.collect(self.settings, self.state)
        n_errors = len(self.state["feed_errors"])
        if not candidates:
            if forced:
                self.tg.send(f"🔎 Scout finished: no new stories found in the last "
                             f"{self.settings['sources'].get('max_age_hours', 24)} h"
                             + (f" ({n_errors} sources failed, send /status)" if n_errors else "")
                             + ". Stories already checked earlier are skipped.")
            return
        editor = Editor(self.secrets.anthropic_key, self.settings)
        recent = [p["headline"] for p in self.state["published"]]
        recent += [d["headline"] for d in self.state["drafts"].values()]
        recent += [d["headline"] for d in self.state["queue"]]
        room = min(sch["drafts_per_scout"], sch["max_drafts_per_day"] - self.drafts_today())
        picks = editor.select(candidates, recent, room)
        log(f"Found {len(candidates)} new stories; {len(picks)} passed the score bar")

        # Everything that was evaluated counts as seen, so it isn't scored again.
        now = time.time()
        scoring_failed = not editor.last_scores and bool(editor.last_error)
        if not scoring_failed:   # if the AI's reply couldn't be read, try these stories again next time
            for c in candidates:
                self.state["seen"][c.key] = now

        made = 0
        failures = []
        for pick in picks:
            if made >= room:
                break
            story = candidates[pick["i"]]
            story.text = sources.fetch_article_text(story)
            log(f"Writing ({pick['score']}/10): {story.title} | article text: {len(story.text)} chars")
            post = editor.write(story, pick["category"])
            if not post:
                log(f"  -> not drafted: {editor.last_error}")
                failures.append((story.title, editor.last_error))
                if "skip" not in editor.last_error:
                    self.state["seen"].pop(story.key, None)   # technical failure: try again next scout
                continue
            try:
                self.make_draft(story, post, pick["score"])
            except Exception as ex:
                log(f"  -> draft failed: {ex}")
                failures.append((story.title, f"error while making the draft: {ex}"))
                self.state["seen"].pop(story.key, None)   # try again next scout
                continue
            log(f"  -> drafted with {len(post.get('slides') or [])} story slides")
            made += 1

        if forced and made == 0:
            min_score = self.settings["ai"].get("min_score", 6)
            top = sorted(editor.last_scores, reverse=True)[:5]
            lines = [f"🔎 Scout finished: checked {len(candidates)} new stories, "
                     f"but none was strong enough to draft (minimum score {min_score}/10)."]
            if room <= 0:
                lines = [f"🔎 Daily draft limit reached ({sch['max_drafts_per_day']}). Try again later."]
            elif not editor.last_scores and editor.last_error:
                lines = [f"🔎 Scout checked {len(candidates)} new stories, but {esc(editor.last_error[:250])}",
                         "\nThe stories weren't marked as seen, so the next scout will try them again."]
            elif failures:
                lines = [f"🔎 Scout finished: {len(picks)} stories passed the {min_score}/10 bar, "
                         "but none could be turned into a draft:"]
                lines += [f"• {esc(t[:80])}: {esc(r[:160])}" for t, r in failures]
            elif top:
                lines.append("\nTop scores:")
                lines += [f"• {sc}/10: {esc(t[:90])}" for sc, t in top]
                lines.append("\nTo get more drafts, lower <code>min_score</code> in config.yaml.")
            if n_errors:
                lines.append(f"⚠️ {n_errors} sources failed: send /status for details.")
            self.tg.send("\n".join(lines))

    def pick_background(self):
        """A random screenshot from the Cloudinary folder set in config.yaml (not one used recently)."""
        cfg = self.settings["design"].get("background_images") or {}
        folder = cfg.get("cloudinary_folder")
        if not folder:
            return None
        if self._bg_urls is None:
            try:
                self._bg_urls = storage.list_images(folder)
                log(f"Background images in '{folder}': {len(self._bg_urls)}")
            except Exception as ex:
                log(f"Couldn't list background images: {ex}")
                self._bg_urls = []
        if not self._bg_urls:
            return None
        recent = self.state.setdefault("recent_backgrounds", [])
        url = random.choice([u for u in self._bg_urls if u not in recent] or self._bg_urls)
        recent.append(url)
        del recent[:-max(1, min(30, len(self._bg_urls) // 2))]
        self._last_bg_url = url
        return self.download_background(url)

    def download_background(self, url):
        path = OUT / f"bg-{hashlib.sha1(url.encode()).hexdigest()[:10]}.jpg"
        if not path.exists():
            OUT.mkdir(parents=True, exist_ok=True)
            r = requests.get(url, timeout=60)
            r.raise_for_status()
            path.write_bytes(r.content)
        return path

    def build_reel(self, did, headline, category, source, slides, background):
        """Render the Reel and upload it. Returns (path, url)."""
        path = reel.make_reel(headline, category, source, slides, self.settings, self.handle(),
                              OUT / f"{did}-reel.mp4", seed=int(did[:6], 16), background=background)
        return path, storage.upload_video(path, f"{did}-reel")

    def make_draft(self, story, post, score):
        did = uuid.uuid4().hex[:10]
        handle = self.handle()
        slides = post.get("slides") or []
        self._last_bg_url = None
        try:
            background = self.pick_background()
        except Exception as ex:
            log(f"  -> no background image: {ex}")
            background = None
        cover = design.render(post["headline"], post["category"], story.source, self.settings,
                              handle, OUT / f"{did}-0.jpg", swipe=bool(slides), background=background)
        paths = [cover]
        image_urls = [storage.upload(cover, f"{did}-0")]
        for n, sl in enumerate(slides, start=2):
            path = design.render_slide(n, len(slides) + 1, sl["title"], sl["text"], story.source,
                                       self.settings, handle, OUT / f"{did}-{n - 1}.jpg", background=background)
            paths.append(path)
            image_urls.append(storage.upload(path, f"{did}-{n - 1}"))
        image_url = image_urls[0]

        # The strongest stories also get a vertical Reel (best format for reaching new people).
        reel_path = reel_url = None
        reel_problem = ""
        rcfg = self.settings.get("reels", {})
        if rcfg.get("enabled") and score >= rcfg.get("min_score", 8):
            if len(slides) >= 2:
                try:
                    log("  -> making a Reel")
                    reel_path, reel_url = self.build_reel(did, post["headline"], post["category"],
                                                          story.source, slides, background)
                except Exception as ex:
                    log(f"  -> Reel failed: {ex}")
                    reel_problem = f"\n🎬 Couldn't make the Reel: <code>{esc(str(ex)[:200])}</code>"
                    reel_path = reel_url = None
            else:
                log("  -> no Reel: not enough story slides")
        tags = " ".join("#" + t for t in post["hashtags"])
        full_caption = f"{post['caption'].strip()}\n\n{tags}".strip()
        draft = {
            "id": did, "created": time.time(), "headline": post["headline"], "url": story.url,
            "source": story.source, "image_url": image_url, "image_urls": image_urls, "full_caption": full_caption,
            "alt_text": post.get("alt_text", ""), "score": score, "reel_url": reel_url,
            # kept so a Reel can be made later with the "Make Reel" button
            "category": post["category"], "slides": slides, "background_url": self._last_bg_url,
        }

        rumour = "" if post.get("confirmed", True) else "\n⚠️ <b>Not officially confirmed</b>"
        note = f"\n📝 {esc(post['note'])}" if post.get("note") else ""
        kind = f"carousel, {len(image_urls)} slides" if len(image_urls) > 1 else "single image"
        info = (f"<b>{esc(post['category'])}</b> · score {score}/10 · {kind}\n"
                f"<a href=\"{esc(story.url)}\">{esc(story.source)}: read the source</a>")
        if len(image_urls) > 1:
            self.tg.send_album_files(paths, info)
        else:
            self.tg.send_photo_file(paths[0], info)
        if reel_path:
            self.tg.send_video_file(reel_path, "🎬 <b>Reel version</b>: posted at the next free slot after the carousel")
        extra = reel_problem

        if not self.settings["approval"]["required"]:
            self.queue_post(draft, post_now=False, with_reel=True)
            self.state["drafts_sent"].append(time.time())
            self.tg.send(f"🤖 Queued automatically:\n\n{esc(full_caption)}{rumour}{note}{extra}")
            return

        msg = self.tg.send(f"{esc(full_caption)}{rumour}{note}{extra}", buttons=self.draft_buttons(draft))
        draft["text_message_id"] = msg["message_id"]
        self.state["drafts"][did] = draft
        self.state["drafts_sent"].append(time.time())

    # ---------- main ----------
    def run(self):
        try:
            self.process_telegram()
            self.expire_drafts()
            self.publish_next()
            if self.should_scout():
                log("Scouting for news")
                self.scout()
                self.publish_next()   # a "post now" approval might be waiting
            else:
                log("No scout this run")
        except Exception as ex:
            traceback.print_exc()
            try:
                self.tg.send(f"💥 The bot hit an error:\n<code>{esc(str(ex))[:1500]}</code>")
            except Exception:
                pass
            raise
        finally:
            state_mod.save(self.state)


def check():
    """Test every connection and report the results to Telegram."""
    settings = load_settings()
    secrets = load_secrets(required=False)
    report = ["<b>🔧 Connection check</b>"]

    def line(ok, name, detail=""):
        report.append(f"{'✅' if ok else '❌'} {name}" + (f": {esc(detail)}" if detail else ""))

    tg = None
    try:
        tg = Telegram(secrets.telegram_token, secrets.telegram_chat_id)
        me = tg.get_me()
        line(True, "Telegram bot", "@" + me["username"])
    except Exception as ex:
        line(False, "Telegram bot", str(ex))

    try:
        ig = Instagram(secrets.ig_user_id, secrets.ig_token, settings["instagram"].get("api_version", "v25.0"))
        acc = ig.account()
        line(True, "Instagram account", f"@{acc.get('username')} ({acc.get('followers_count', '?')} followers)")
        q = ig.quota()
        line(True, "Instagram publishing", f"{q.get('quota_usage', 0)} posts used in the last 24h")
    except Exception as ex:
        line(False, "Instagram", str(ex))

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=secrets.anthropic_key)
        client.messages.create(model=settings["ai"]["model"], max_tokens=5,
                               messages=[{"role": "user", "content": "Say ok"}])
        line(True, "Claude API", settings["ai"]["model"])
    except Exception as ex:
        line(False, "Claude API", str(ex)[:200])

    try:
        storage.configure(secrets)
        storage.ping()
        line(True, "Cloudinary", secrets.cloudinary_name)
    except Exception as ex:
        line(False, "Cloudinary", str(ex)[:200])

    tmp = {**state_mod.load(), "seen": {}}
    try:
        stories = sources.collect(settings, tmp)
        line(bool(stories), "News sources", f"{len(stories)} fresh stories found")
        for name, err in tmp["feed_errors"].items():
            report.append(f"   ⚠️ {esc(name)}: {esc(err[:80])}")
    except Exception as ex:
        line(False, "News sources", str(ex))

    text = "\n".join(report)
    print(text.replace("<b>", "").replace("</b>", ""))
    if tg:
        try:
            tg.send(text)
        except Exception as ex:
            print("Could not send report to Telegram:", ex)
    if "❌" in text:
        sys.exit(1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "check":
        check()
    elif mode == "scout":
        b = Bot()
        b.state["force_scout"] = True
        b.run()
    else:
        Bot().run()

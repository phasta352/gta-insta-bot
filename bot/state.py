"""Small JSON 'memory' that is committed back to the repo after every run.

GitHub Actions starts a fresh machine each time, so anything the bot needs to
remember (stories already seen, drafts waiting for approval, the post queue)
lives in state/state.json.
"""
import json
import time
from pathlib import Path

from .config import ROOT

STATE_FILE = ROOT / "state" / "state.json"

DEFAULT = {
    "telegram_offset": 0,       # last Telegram update processed
    "paused": False,
    "force_scout": False,
    "last_scout": 0,
    "seen": {},                 # story key -> timestamp first seen
    "drafts": {},               # draft id -> draft (waiting for your decision)
    "queue": [],                # approved drafts waiting for a posting slot
    "published": [],            # recent posts {id, headline, url, at, media_id}
    "drafts_sent": [],          # timestamps of drafts sent (for the daily cap)
    "feed_errors": {},          # feed name -> last error message
    "ig_handle": "",
}


def load() -> dict:
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    else:
        data = {}
    for k, v in DEFAULT.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def save(state: dict) -> None:
    prune(state)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def prune(state: dict) -> None:
    """Keep the file small: forget old items."""
    now = time.time()
    week = 7 * 86400
    state["seen"] = {k: t for k, t in state["seen"].items() if now - t < 2 * week}
    state["published"] = state["published"][-200:]
    state["drafts_sent"] = [t for t in state["drafts_sent"] if now - t < 86400 * 2]

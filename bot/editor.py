"""The AI editor: picks the best stories and writes the post text with Claude."""
import json
import re

import anthropic

# Default categories (cycling). A page can set its own list under ai.categories in config.yaml.
DEFAULT_CATEGORIES = ["RACE NEWS", "RESULT", "TRANSFER", "TEAM NEWS", "RIDER NEWS", "GEAR & TECH", "UCI", "BREAKING"]
DEFAULT_PRIORITIES = ("Prefer big names, big races, official announcements, results, transfers and "
                      "notable product launches.")


class Editor:
    def __init__(self, api_key: str, settings: dict):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.settings = settings
        self.model = settings["ai"]["model"]
        self.categories = settings["ai"].get("categories") or DEFAULT_CATEGORIES
        self.last_scores = []
        self.last_error = ""
        self.last_stop = ""
        self.last_raw = ""

    def _ask(self, system: str, prompt: str, max_tokens: int = 1500) -> dict | list:
        msg = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        self.last_stop = getattr(msg, "stop_reason", "")
        self.last_raw = text
        return parse_json(text)

    def _brief(self) -> str:
        p = self.settings["page"]
        return (f"You are the editor of the Instagram page \"{p['name']}\".\n"
                f"Focus: {p['focus'].strip()}\nAvoid: {p['avoid'].strip()}\n"
                f"Language: {p['language']}.")

    def select(self, stories, recent_headlines: list[str], limit: int) -> list[dict]:
        """Score candidate stories and return the best few as [{i, score, category}]."""
        lines = []
        for i, s in enumerate(stories):
            summary = f" | {s.summary[:220]}" if s.summary else ""
            lines.append(f"[{i}] ({s.source}) {s.title}{summary}")
        recent = "\n".join(f"- {h}" for h in recent_headlines[-30:]) or "(none yet)"
        system = self._brief() + (
            "\nYour job: choose which news stories would make the most engaging, shareable "
            f"Instagram posts for {self.settings['page'].get('audience', 'cycling fans')} today. "
            f"{self.settings['page'].get('priorities', DEFAULT_PRIORITIES).strip()} Several headlines "
            "may describe the same event: treat them as one and pick the most informative. "
            "Never pick a story the page has already covered. Reply with JSON only."
        )
        prompt = (
            f"Already posted recently (do NOT repeat these events):\n{recent}\n\n"
            f"Candidate headlines:\n" + "\n".join(lines) + "\n\n"
            "Return a JSON list of the stories worth posting, best first, at most "
            f"{limit + 3} items. Each item: {{\"i\": <index>, \"score\": <1-10 engagement score>, "
            f"\"category\": one of {self.categories}, \"duplicates\": [<up to 5 indexes of other headlines about the same event>]}}. "
            "Keep it compact: no explanations, no text outside the JSON. "
            "Return [] if nothing is worth posting."
        )
        self.last_error = ""
        result = self._ask(system, prompt, max_tokens=3000)
        self.last_scores = []   # (score, headline) for every story the AI rated, for reports
        if isinstance(result, dict):   # e.g. {"stories": [...]}: use the first list inside
            result = next((v for v in result.values() if isinstance(v, list)), None)
        if not isinstance(result, list):
            self.last_error = ("the AI's scoring reply was cut off" if self.last_stop == "max_tokens"
                               else "couldn't read the AI's scoring reply") + f": {self.last_raw[:200]!r}"
            print(f"[bot] Scoring failed. stop_reason={self.last_stop}. Reply starts: {self.last_raw[:500]!r}", flush=True)
            return []
        print(f"[bot] AI scored {len(result)} stories (stop_reason={self.last_stop})", flush=True)
        for item in result:
            try:
                self.last_scores.append((int(item.get("score", 0)), stories[int(item["i"])].title))
            except (KeyError, ValueError, TypeError, IndexError):
                pass
        min_score = self.settings["ai"].get("min_score", 6)
        picks = []
        for item in result:
            try:
                i = int(item["i"])
                if 0 <= i < len(stories) and int(item.get("score", 0)) >= min_score:
                    picks.append({"i": i, "score": int(item["score"]),
                                  "category": str(item.get("category", self.categories[0])).upper(),
                                  "duplicates": [int(d) for d in item.get("duplicates", []) if str(d).isdigit()]})
            except (KeyError, ValueError, TypeError):
                continue
        return picks

    def write(self, story, category: str) -> dict | None:
        """Write the graphic headline, caption, hashtags and alt text for one story."""
        p = self.settings["page"]
        facts = story.text or story.summary or "(only the headline is available)"
        car = self.settings.get("carousel", {})
        max_slides = max(0, min(int(car.get("max_slides", 4)), 9)) if car.get("enabled", True) else 0
        if max_slides >= 2:
            slides_rule = (
                f"list of 2-{max_slides} story slides for an Instagram carousel that follows the cover, "
                "telling the story step by step (e.g. what happened, key details, background, why it matters). "
                "Each slide: {\"title\": max 40 characters, \"text\": 1-3 sentences, max 230 characters}. "
                "Only facts from the material. If the material is too thin for at least 2 useful slides, return []. "
                "When there are slides, keep the caption shorter (max 500 characters) since the slides carry the story."
            )
        else:
            slides_rule = "always return []"
        system = self._brief() + (
            f"\nTone: {p['tone'].strip()}\n"
            "Rules: use ONLY facts stated in the source material below; never invent results, "
            "times, quotes, prices or dates. Write everything in your own words (no copied "
            "sentences, no quotes longer than a few words). If the material is clearly a rumour, "
            "say so or set \"confirmed\" to false. If there isn't enough information for a "
            "useful post, set \"skip\" to true. Never skip just because there isn't enough for "
            "carousel slides: return \"slides\": [] and it becomes a single-image post. Reply with JSON only."
        )
        prompt = (
            f"Source: {story.source}\nHeadline: {story.title}\nURL: {story.url}\n"
            f"Material:\n{facts}\n\n"
            "Return a JSON object with:\n"
            "- \"headline\": punchy headline for the graphic, max 70 characters, no emoji, no hashtags\n"
            f"- \"category\": short label, one of {self.categories} (suggested: {category})\n"
            "- \"caption\": Instagram caption: a strong first line, then 2-4 short sentences explaining "
            "the news and why it matters, then a question to invite comments. Max 900 characters. "
            "End with a line \"Source: <publication name>\". No hashtags in the caption\n"
            "- \"hashtags\": list of 6-10 relevant hashtags without the # sign (mix big and niche)\n"
            "- \"alt_text\": one-sentence description of the graphic for visually impaired users\n"
            "- \"confirmed\": true if the news is officially confirmed, false if rumour/report\n"
            "- \"skip\": true if this should not be posted\n"
            "- \"note\": one short line for the page owner about anything to double-check (or \"\")\n"
            f"- \"slides\": {slides_rule}"
        )
        self.last_error = ""
        result = self._ask(system, prompt, max_tokens=4000)
        if not isinstance(result, dict):
            self.last_error = ("the AI's reply was cut off" if self.last_stop == "max_tokens"
                               else "couldn't read the AI's reply") + f": {self.last_raw[:120]!r}"
            return None
        if result.get("skip"):
            self.last_error = "the AI chose to skip it" + (f" ({result.get('note')})" if result.get("note") else "")
            return None
        if not result.get("headline") or not result.get("caption"):
            self.last_error = "the AI's reply had no headline or caption"
            return None
        result["headline"] = result["headline"].strip()[:90]
        result["category"] = str(result.get("category") or category).upper()[:20]
        tags = [re.sub(r"[^\w]", "", t.lstrip("#")) for t in result.get("hashtags", [])]
        result["hashtags"] = [t for t in tags if t][:12]
        slides = []
        for sl in result.get("slides") or []:
            if isinstance(sl, dict) and sl.get("title") and sl.get("text"):
                slides.append({"title": str(sl["title"]).strip()[:60], "text": str(sl["text"]).strip()[:320]})
        result["slides"] = slides[:max_slides] if len(slides) >= 2 else []
        return result


def parse_json(text: str):
    """Pull the first JSON object/list out of the model's reply."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.S)
    if fence:
        text = fence.group(1).strip()
    pairs = [("[", "]"), ("{", "}")]
    # Try whichever bracket comes first, so an object containing a list parses as the object.
    pairs.sort(key=lambda p: text.find(p[0]) if text.find(p[0]) != -1 else len(text))
    for opener, closer in pairs:
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            candidate = text[start:end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

"""Loads config.yaml and the secrets stored in GitHub (environment variables)."""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# Names of the secrets exactly as they are set up in the GitHub repository.
SECRET_NAMES = {
    "anthropic_key": "ANTHROPIC_API_KEY",
    "cloudinary_name": "CLOUDINARY_NAME",
    "cloudinary_key": "CLOUDINARY_API_KEY",
    "cloudinary_secret": "CLOUDINARY_API_SECRET",
    "ig_user_id": "INSTAGRAM_APP_ID",        # the Instagram account ID
    "ig_token": "INSTAGRAM_APP_TOKEN",       # system-user access token (never expires)
    "telegram_chat_id": "TELEGRAM_BOT_ID",   # your personal chat ID
    "telegram_token": "TELEGRAM_BOT_TOKEN",
}


@dataclass
class Secrets:
    anthropic_key: str
    cloudinary_name: str
    cloudinary_key: str
    cloudinary_secret: str
    ig_user_id: str
    ig_token: str
    telegram_chat_id: str
    telegram_token: str


def load_settings(path: Path = ROOT / "config.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_secrets(required: bool = True) -> Secrets:
    values, missing = {}, []
    for field, env_name in SECRET_NAMES.items():
        val = os.environ.get(env_name, "").strip()
        if not val:
            missing.append(env_name)
        values[field] = val
    if missing and required:
        raise SystemExit(
            "Missing GitHub secrets: " + ", ".join(missing)
            + "\nAdd them under Settings > Secrets and variables > Actions, "
            "and make sure the workflow file passes them in."
        )
    return Secrets(**values)

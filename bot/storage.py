"""Uploads graphics to Cloudinary so Instagram can fetch them from a public link."""
from pathlib import Path

import cloudinary
import cloudinary.api
import cloudinary.uploader


FOLDER = "cycling-bot"   # replaced by cloudinary.upload_folder in config.yaml


def configure(secrets, settings=None) -> None:
    global FOLDER
    if settings:
        FOLDER = settings.get("cloudinary", {}).get("upload_folder", FOLDER)
    cloudinary.config(
        cloud_name=secrets.cloudinary_name,
        api_key=secrets.cloudinary_key,
        api_secret=secrets.cloudinary_secret,
        secure=True,
    )


def upload(path: Path, public_id: str) -> str:
    res = cloudinary.uploader.upload(
        str(path), folder=FOLDER, public_id=public_id, overwrite=True, resource_type="image"
    )
    return res["secure_url"]


def upload_video(path: Path, public_id: str) -> str:
    res = cloudinary.uploader.upload(
        str(path), folder=FOLDER, public_id=public_id, overwrite=True, resource_type="video"
    )
    return res["secure_url"]


def list_images(folder: str) -> list[str]:
    """URLs of the images in a Cloudinary folder (works with both old and new folder modes)."""
    urls = []
    try:   # accounts with "dynamic folders" (most new accounts)
        res = cloudinary.api.resources_by_asset_folder(folder, max_results=500)
        urls = [r["secure_url"] for r in res.get("resources", []) if r.get("resource_type") == "image"]
    except Exception:
        pass
    if not urls:   # older accounts: folders are part of the image name
        res = cloudinary.api.resources(type="upload", prefix=folder.rstrip("/") + "/", max_results=500,
                                       resource_type="image")
        urls = [r["secure_url"] for r in res.get("resources", [])]
    return urls


def ping() -> str:
    cloudinary.api.ping()
    return "ok"

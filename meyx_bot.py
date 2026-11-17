import asyncio
import json
import os
import re
import subprocess
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# ==========================================
# SETTINGS
# ==========================================
TARGET_USER = "meyxdollw"
MAX_AGE_SECONDS = 21600  # 6 hours
PING_USER_ID = "644995150774272020"
MAX_DISCORD_FILE_MB = 9.5

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_MEYX")
MEMORY_FILE = "seen_meyx_posts.txt"
TZ = ZoneInfo("Europe/Brussels")


def load_seen() -> set:
    if not os.path.exists(MEMORY_FILE):
        return set()
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_seen(item_id: str):
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")


def get_tiktok_timestamp(video_id: str) -> int:
    try:
        return int(video_id) >> 32
    except Exception:
        return 0


def post_to_discord(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_MEYX secret is missing!")
        return False
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        print(f"   [DISCORD] Status {res.status_code}")
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"   [DISCORD ERROR] {e}")
        return False


def has_video_stream(file_path: str) -> bool:
    try:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1",
            file_path
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return bool(res.stdout.strip())
    except Exception:
        return True


def compress_to_fit_discord(input_path: str, output_path: str) -> bool:
    try:
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-vf", "scale='min(720,iw)':-2",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-movflags", "+faststart",
            output_path
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=True)
        return os.path.exists(output_path) and os.path.getsize(output_path) > 10000
    except Exception as e:
        print(f"   [!] Compression failed: {e}")
        return False


def download_tiktok_video(post_url: str, video_id: str) -> str | None:
    raw_path = f"raw_{video_id}.mp4"
    comp_path = f"comp_{video_id}.mp4"

    for p in (raw_path, comp_path):
        if os.path.exists(p):
            os.remove(p)

    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4][vcodec^=avc]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", raw_path,
        post_url,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=75)
        if not os.path.exists(raw_path):
            return None

        if not has_video_stream(raw_path):
            os.remove(raw_path)
            return None

        size_mb = os.path.getsize(raw_path) / (1024 * 1024)
        if size_mb <= MAX_DISCORD_FILE_MB:
            return raw_path

        if compress_to_fit_discord(raw_path, comp_path):
            os.remove(raw_path)
            if os.path.getsize(comp_path) / (1024 * 1024) <= MAX_DISCORD_FILE_MB:
                return comp_path
            os.remove(comp_path)
    except Exception as e:
        print(f"   [!] Video download failed: {e}")
        for p in (raw_path, comp_path):
            if os.path.exists(p):
                os.remove(p)
    return None


def download_slideshow_images(post_url: str, video_id: str) -> list[str]:
    saved_files = []
    try:
        res = requests.get("https://www.tikwm.com/api/", params={"url": post_url, "hd": 1}, timeout=20)
        images = res.json().get("data", {}).get("images", [])
        for idx, img_url in enumerate(images[:10]):
            img_res = requests.get(img_url, timeout=15)
            if img_res.status_code == 200:
                img_path = f"photo_{video_id}_{idx + 1}.jpg"
                with open(img_path, "wb") as f:
                    f.write(img_res.content)
                saved_files.append(img_path)
    except Exception as e:
        print(f"   [!] Slideshow download error: {e}")
    return saved_files


def send_alert(username: str, post_url: str, video_id: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_MEYX secret is missing!")
        return False

    has_id = PING_USER_ID.isdigit()
    mention = f"<@{PING_USER_ID}>" if has_id else ""
    allowed_mentions = {"users": [PING_USER_ID] if has_id else []}

    video_path = download_tiktok_video(post_url, video_id)
    if video_path:
        try:
            payload = {
                "username": "Alert Bot",
                "content": f"{mention} nouvelle vidéo de **@{username}**".strip(),
                "allowed_mentions": allowed_mentions,
            }
            with open(video_path, "rb") as f:
                res = requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"payload_json": json.dumps(payload)},
                    files={"file": (f"video_{video_id}.mp4", f, "video/mp4")},
                    timeout=60,
                )
            os.remove(video_path)
            if res.status_code in (200, 204):
                return True
        except Exception as e:
            print(f"   [DISCORD UPLOAD ERROR] {e}")
            if os.path.exists(video_path):
                os.remove(video_path)

    photo_paths = download_slideshow_images(post_url, video_id)
    if photo_paths:
        open_files = []
        try:
            payload = {
                "username": "Alert Bot",
                "content": f"{mention} nouveau post photo ({len(photo_paths)} photos) de **@{username}**".strip(),
                "allowed_mentions": allowed_mentions,
            }
            files_payload = {}
            for i, p in enumerate(photo_paths):
                f_obj = open(p, "rb")
                open_files.append(f_obj)
                files_payload[f"file{i}"] = (os.path.basename(p), f_obj, "image/jpeg")

            res = requests.post(
                DISCORD_WEBHOOK_URL,
                data={"payload_json": json.dumps(payload)},
                files=files_payload,
                timeout=60,
            )
            for f_obj in open_files:
                f_obj.close()
            for p in photo_paths:
                if os.path.exists(p):
                    os.remove(p)
            if res.status_code in (200, 204):
                return True
        except Exception as e:
            print(f"   [DISCORD PHOTO UPLOAD ERROR] {e}")
            for f_obj in open_files:
                try:
                    f_obj.close()
                except Exception:
                    pass
            for p in photo_paths:
                if os.path.exists(p):
                    os.remove(p)

    embed_video_url = post_url.replace("www.tiktok.com", "tnktok.com")
    payload = {
        "username": "Alert Bot",
        "content": f"{mention} nouveau post de **@{username}**\n{embed_video_url}".strip(),
        "allowed_mentions": allowed_mentions,
    }
    return post_to_discord(payload)


def get_tiktok_via_ytdlp(user: str) -> list:
    video_ids = []
    try:
        cmd = [
            "yt-dlp", "--flat-playlist", "--playlist-end", "15", "--print", "id",
            f"https://www.tiktok.com/@{user}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in res.stdout.splitlines():
            clean = line.strip()
            if clean.isdigit() and len(clean) >= 15 and clean not in video_ids:
                video_ids.append(clean)
    except Exception as e:
        print(f"   [INFO] yt-dlp skipped: {e}")
    return video_ids


async def get_tiktok_via_browser(user: str) -> list:
    video_ids = []
    async with Stealth().use_async(async_playwright()) as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
            timezone_id="Europe/Brussels",
        )
        page = await context.new_page()
        try:
            await page.goto(f"https://www.tiktok.com/@{user}", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(4000)
            await page.evaluate("window.scrollBy(0, 1000)")
            await page.wait_for_timeout(1500)
            html = await page.content()
            pattern = rf"/(?:@{re.escape(user)}/)?(?:video|photo)/(\d{{15,22}})"
            for m in re.findall(pattern, html):
                if m not in video_ids:
                    video_ids.append(m)
        except Exception as e:
            print(f"   [!] Browser error: {e}")
        await browser.close()
    return video_ids[:15]


async def main():
    now_be = datetime.now(TZ)
    now_ts = int(time.time())
    print(f"=== CHECKING @{TARGET_USER} ({now_be.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    seen = load_seen()

    # Merge both sources so cached yt-dlp results never hide fresh browser posts
    ytdlp_ids = get_tiktok_via_ytdlp(TARGET_USER)
    browser_ids = await get_tiktok_via_browser(TARGET_USER)
    all_ids = list(dict.fromkeys(ytdlp_ids + browser_ids))

    print(f"   {len(all_ids)} unique posts found (yt-dlp: {len(ytdlp_ids)}, browser: {len(browser_ids)})")

    for vid in all_ids:
        key = f"TT_{vid}"
        if key in seen:
            continue

        vid_ts = get_tiktok_timestamp(vid)
        age_seconds = now_ts - vid_ts if vid_ts > 0 else None
        post_url = f"https://www.tiktok.com/@{TARGET_USER}/video/{vid}"

        if age_seconds is not None and age_seconds <= MAX_AGE_SECONDS:
            print(f"   🔥 NEW POST ({age_seconds // 60} min ago): {post_url}")
            if send_alert(TARGET_USER, post_url, vid):
                save_seen(key)
                seen.add(key)
        else:
            age_min = (age_seconds // 60) if age_seconds is not None else -1
            print(f"   [SKIP] {vid} ({age_min} min ago)")
            save_seen(key)
            seen.add(key)

    print("=== CHECK COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())

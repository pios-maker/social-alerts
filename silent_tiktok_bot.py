import asyncio
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
TIKTOK_USERS = [
    "tytyshi",
    "._leave.me_alone",
    "meyxdollw",
    "mahsa9946",
    "apparentlysalma",
]

MAX_AGE_SECONDS = 10800  # 3 hours
PING_USER_ID = "644995150774272020"

# Use a separate webhook secret if you want this in a different channel
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_SILENT")
MEMORY_FILE = "seen_silent_posts.txt"
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


def send_minimal_alert() -> bool:
    """Sends ONLY '<@PING_USER_ID> new video posted' with no author, link, or media."""
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_SILENT secret is missing!")
        return False

    has_id = PING_USER_ID.isdigit()
    mention = f"<@{PING_USER_ID}>" if has_id else ""
    allowed_mentions = {"users": [PING_USER_ID] if has_id else []}

    payload = {
        "username": "Alert Bot",
        "content": f"{mention} new video posted".strip(),
        "allowed_mentions": allowed_mentions,
    }
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        print(f"   [DISCORD] Status {res.status_code}")
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"   [DISCORD ERROR] {e}")
        return False


def get_tiktok_via_ytdlp(user: str) -> list:
    video_ids = []
    try:
        cmd = [
            "yt-dlp", "--flat-playlist", "--playlist-end", "6", "--print", "id",
            f"https://www.tiktok.com/@{user}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in res.stdout.splitlines():
            clean = line.strip()
            if clean.isdigit() and len(clean) >= 15 and clean not in video_ids:
                video_ids.append(clean)
    except Exception as e:
        print(f"   [INFO] yt-dlp check skipped for @{user}: {e}")
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
            await page.wait_for_timeout(3500)
            html = await page.content()
            pattern = rf"/(?:@{re.escape(user)}/)?(?:video|photo)/(\d{{15,22}})"
            for m in re.findall(pattern, html):
                if m not in video_ids:
                    video_ids.append(m)
        except Exception as e:
            print(f"   [!] TikTok browser error for @{user}: {e}")
        await browser.close()
    return video_ids[:6]


async def main():
    now_be = datetime.now(TZ)
    now_ts = int(time.time())
    print(f"=== START SILENT TIKTOK CHECK ({now_be.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    seen = load_seen()

    for user in TIKTOK_USERS:
        print(f"-> Checking @{user}")
        tt_ids = get_tiktok_via_ytdlp(user)
        if not tt_ids:
            print("   yt-dlp returned nothing, trying browser...")
            tt_ids = await get_tiktok_via_browser(user)

        print(f"   {len(tt_ids)} posts read")
        if not tt_ids:
            continue

        for vid in tt_ids:
            key = f"TT_{vid}"
            if key in seen:
                continue

            vid_ts = get_tiktok_timestamp(vid)
            age_seconds = now_ts - vid_ts if vid_ts > 0 else None

            if age_seconds is not None and age_seconds <= MAX_AGE_SECONDS:
                print(f"   🔥 NEW POST DETECTED ({age_seconds // 60} min ago)")
                if send_minimal_alert():
                    save_seen(key)
                    seen.add(key)
            else:
                save_seen(key)
                seen.add(key)

    print("=== CHECK COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())

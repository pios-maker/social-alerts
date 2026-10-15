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
TIKTOK_USER = "tytyshi"
TIKTOK_URL = f"https://www.tiktok.com/@{TIKTOK_USER}"

# A video only triggers an alert if it was published less than 3 hours ago
MAX_AGE_SECONDS = 10800

# Discord user ID of katchu_ (a number like 123456789012345678).
# To get it: Discord > Settings > Advanced > enable Developer Mode,
# then right-click katchu_ > "Copy User ID".
PING_USER_ID = "644995150774272020"

# True  = send a silent "check done, no new video" message after every check
# False = only send messages when there is a new video
SEND_STATUS_EVERY_CHECK = True

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_SOCIAL")
MEMORY_FILE = "seen_social_posts.txt"
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
    """Publication time hidden in the TikTok video ID (Unix timestamp)."""
    try:
        return int(video_id) >> 32
    except Exception:
        return 0


def post_to_discord(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_SOCIAL secret is missing!")
        return False
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"   [DISCORD] Status {res.status_code}")
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"   [DISCORD ERROR] {e}")
        return False


def send_video_alert(username: str, post_url: str, now_str: str) -> bool:
    """New video: pings katchu_."""
    has_id = PING_USER_ID.isdigit()
    mention = f"<@{PING_USER_ID}>" if has_id else "@katchu_"

    embed = {
        "title": f"🎵 New TikTok video from @{username}!",
        "description": (
            f"**@{username}** just published a new video on **TikTok**!\n\n"
            f"👉 **[Click here to watch it on TikTok]({post_url})**"
        ),
        "url": post_url,
        "color": 16711760,
        "footer": {"text": f"TikTok Monitor • Detected at {now_str}"},
    }
    payload = {
        "username": "Tytyshi Alert Bot",
        "content": f"{mention} 🎵 **{username} posted a new video on TikTok!**",
        "embeds": [embed],
        # Only this user can be pinged (never @everyone)
        "allowed_mentions": {"users": [PING_USER_ID] if has_id else []},
    }
    return post_to_discord(payload)


def send_status(text: str, color: int, now_str: str) -> bool:
    """Silent message: no mention at all."""
    payload = {
        "username": "Tytyshi Alert Bot",
        "embeds": [{
            "description": text,
            "color": color,
            "footer": {"text": f"TikTok Monitor • {now_str}"},
        }],
        "allowed_mentions": {"parse": []},
    }
    return post_to_discord(payload)


def get_tiktok_via_ytdlp() -> list:
    video_ids = []
    try:
        cmd = ["yt-dlp", "--flat-playlist", "--playlist-end", "6", "--print", "id", TIKTOK_URL]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in res.stdout.splitlines():
            clean = line.strip()
            if clean.isdigit() and len(clean) >= 15 and clean not in video_ids:
                video_ids.append(clean)
    except Exception as e:
        print(f"   [INFO] yt-dlp TikTok check skipped: {e}")
    return video_ids


async def get_tiktok_via_browser() -> list:
    """Plan B: open the profile in a browser and look for video links."""
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
            await page.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3500)
            html = await page.content()
            for m in re.findall(rf"/@{TIKTOK_USER}/video/(\d{{15,22}})", html):
                if m not in video_ids:
                    video_ids.append(m)
        except Exception as e:
            print(f"   [!] TikTok browser error: {e}")
        await browser.close()
    return video_ids[:6]


async def main():
    now_be = datetime.now(TZ)
    now_str = now_be.strftime("%H:%M")
    now_ts = int(time.time())
    print(f"=== START TIKTOK CHECK ({now_be.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    seen = load_seen()

    tt_ids = get_tiktok_via_ytdlp()
    if not tt_ids:
        print("   yt-dlp returned nothing, trying browser...")
        tt_ids = await get_tiktok_via_browser()

    print(f"-> Checked TikTok ({len(tt_ids)} videos)")

    alerts_sent = 0
    for vid in tt_ids:
        key = f"TT_{vid}"
        if key in seen:
            continue

        vid_ts = get_tiktok_timestamp(vid)
        age_seconds = now_ts - vid_ts if vid_ts > 0 else None
        post_url = f"https://www.tiktok.com/@{TIKTOK_USER}/video/{vid}"

        if age_seconds is not None and age_seconds <= MAX_AGE_SECONDS:
            print(f"   🔥 NEW TIKTOK ({age_seconds // 60} min ago): {post_url}")
            # Only remember it if the alert really went out
            if send_video_alert(TIKTOK_USER, post_url, now_str):
                save_seen(key)
                seen.add(key)
                alerts_sent += 1
        else:
            # Old video: remember it without alerting
            save_seen(key)
            seen.add(key)

    # Silent status message when no video alert was sent
    if SEND_STATUS_EVERY_CHECK and alerts_sent == 0:
        if not tt_ids:
            send_status(
                "⚠️ Check done, but I **could not read the TikTok profile** "
                "(blocked or no response). No video could be checked.",
                16763904, now_str,
            )
        else:
            send_status(
                f"✅ Check done at {now_str} — **no new video** "
                f"({len(tt_ids)} videos checked).",
                3447003, now_str,
            )

    print("=== CHECK COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())

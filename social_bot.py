import asyncio
import os
import re
import subprocess
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

INSTA_USER = "tytyshi_"
INSTA_URL = f"https://www.instagram.com/{INSTA_USER}/"

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


def send_discord_alert(platform: str, username: str, post_url: str, now_str: str, is_initial: bool = False):
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_SOCIAL secret is missing!")
        return

    if platform == "TikTok":
        color = 16711760  # TikTok Pink/Red
        icon = "🎵"
    else:
        color = 14764502  # Instagram Purple/Pink
        icon = "📸"

    if is_initial:
        content_ping = f"✅ **{platform} Monitor is now active for @{username}!**"
        title_text = f"{icon} Tracking started: @{username} ({platform})"
        desc_text = (
            f"The bot has memorized the current profile and will check every hour.\n\n"
            f"🎬 **Latest post on profile right now:**\n👉 **[Click here to watch on {platform}]({post_url})**"
        )
    else:
        content_ping = f"@everyone 🚨 **New {platform} video/post from @{username}!**"
        title_text = f"{icon} NEW {platform.upper()} POST BY @{username}!"
        desc_text = (
            f"**@{username}** just published a new video/post on {platform}!\n\n"
            f"👉 **[Click here to watch it now]({post_url})**"
        )

    embed = {
        "title": title_text,
        "description": desc_text,
        "url": post_url,
        "color": color,
        "footer": {"text": f"Social Media Monitor • Checked at {now_str}"},
    }

    payload = {
        "username": "Tytyshi Alert Bot",
        "content": content_ping,
        "embeds": [embed],
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"   [DISCORD] ✅ Alert sent for {platform} ({post_url}) -> Status {res.status_code}")
    except Exception as e:
        print(f"   [DISCORD ERROR] {e}")


def get_tiktok_via_ytdlp() -> list:
    video_ids = []
    try:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--playlist-end", "5",
            "--print", "id",
            TIKTOK_URL,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        for line in res.stdout.splitlines():
            clean = line.strip()
            if clean.isdigit() and len(clean) >= 15 and clean not in video_ids:
                video_ids.append(clean)
    except Exception as e:
        print(f"   [INFO] yt-dlp TikTok check skipped: {e}")
    return video_ids


async def scrape_socials_with_browser(need_tiktok: bool) -> tuple:
    tiktok_ids = []
    insta_codes = []

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

        if need_tiktok:
            try:
                print(f"   [BROWSER] Scanning TikTok: {TIKTOK_URL}")
                await page.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=35000)
                await page.wait_for_timeout(4000)
                html = await page.content()

                matches = re.findall(rf"/@{TIKTOK_USER}/video/(\d{{15,22}})", html)
                for m in matches:
                    if m not in tiktok_ids:
                        tiktok_ids.append(m)
            except Exception as e:
                print(f"   [!] TikTok browser error: {e}")

        try:
            print(f"   [BROWSER] Scanning Instagram: {INSTA_URL}")
            await page.goto(INSTA_URL, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(4000)

            links = await page.locator("a[href*='/reel/'], a[href*='/p/']").all()
            for link in links:
                href = await link.get_attribute("href")
                if href:
                    m = re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)/?", href)
                    if m and m.group(1) not in insta_codes:
                        insta_codes.append(m.group(1))

            if not insta_codes:
                html = await page.content()
                codes = re.findall(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{8,15})"', html)
                for c in codes:
                    if c not in insta_codes:
                        insta_codes.append(c)
        except Exception as e:
            print(f"   [!] Instagram browser error: {e}")

        await browser.close()

    return tiktok_ids[:5], insta_codes[:5]


async def main():
    now_be = datetime.now(TZ)
    now_str = now_be.strftime("%H:%M")
    print(f"=== START SOCIAL SCAN ({now_be.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    seen = load_seen()
    first_tiktok_run = not any(x.startswith("TT_") for x in seen)
    first_insta_run = not any(x.startswith("IG_") for x in seen)

    tt_ids = get_tiktok_via_ytdlp()
    tt_browser_ids, ig_codes = await scrape_socials_with_browser(need_tiktok=(len(tt_ids) == 0))
    if not tt_ids:
        tt_ids = tt_browser_ids

    print(f"-> TikTok videos found: {len(tt_ids)} | Instagram posts found: {len(ig_codes)}")

    if tt_ids:
        if first_tiktok_run:
            for vid in tt_ids:
                save_seen(f"TT_{vid}")
                seen.add(f"TT_{vid}")
            latest_url = f"https://www.tiktok.com/@{TIKTOK_USER}/video/{tt_ids[0]}"
            send_discord_alert("TikTok", TIKTOK_USER, latest_url, now_str, is_initial=True)
        else:
            for vid in reversed(tt_ids):
                key = f"TT_{vid}"
                if key not in seen:
                    post_url = f"https://www.tiktok.com/@{TIKTOK_USER}/video/{vid}"
                    send_discord_alert("TikTok", TIKTOK_USER, post_url, now_str, is_initial=False)
                    save_seen(key)
                    seen.add(key)

    if ig_codes:
        if first_insta_run:
            for code in ig_codes:
                save_seen(f"IG_{code}")
                seen.add(f"IG_{code}")
            latest_url = f"https://www.instagram.com/p/{ig_codes[0]}/"
            send_discord_alert("Instagram", INSTA_USER, latest_url, now_str, is_initial=True)
        else:
            for code in reversed(ig_codes):
                key = f"IG_{code}"
                if key not in seen:
                    post_url = f"https://www.instagram.com/p/{code}/"
                    send_discord_alert("Instagram", INSTA_USER, post_url, now_str, is_initial=False)
                    save_seen(key)
                    seen.add(key)

    print("=== SOCIAL SCAN COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())

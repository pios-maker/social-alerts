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

INSTA_USER = "tytyshi_"
INSTA_URL = f"https://www.instagram.com/{INSTA_USER}/"

# Durée maximale d'ancienneté d'une vidéo pour envoyer une alerte (en secondes)
# 5400 secondes = 1h30 (marge de sécurité idéale pour un scan toutes les heures)
MAX_AGE_SECONDS = 5400

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
    """Extrait l'heure exacte de publication cachée dans l'ID de la vidéo TikTok."""
    try:
        return int(video_id) >> 32
    except Exception:
        return 0


def send_discord_alert(platform: str, username: str, post_url: str, now_str: str):
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_SOCIAL secret is missing!")
        return

    if platform == "TikTok":
        color = 16711760  # Rose/Rouge TikTok
        icon = "🎵"
        msg_content = f"@everyone 🎵 **{username} a sorti une nouvelle vidéo sur son TikTok !**"
        title_text = f"{icon} New TikTok video from @{username}!"
    else:
        color = 14764502  # Violet/Rose Instagram
        icon = "📸"
        msg_content = f"@everyone 📸 **{username} a sorti une nouvelle vidéo/post sur son Instagram !**"
        title_text = f"{icon} New Instagram post from @{username}!"

    embed = {
        "title": title_text,
        "description": (
            f"**@{username}** just published a new video on **{platform}** (less than 1 hour ago)!\n\n"
            f"👉 **[Click here to watch it on {platform}]({post_url})**"
        ),
        "url": post_url,
        "color": color,
        "footer": {"text": f"Social Media Monitor • Detected at {now_str}"},
    }

    payload = {
        "username": "Tytyshi Alert Bot",
        "content": msg_content,
        "embeds": [embed],
    }

    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"   [DISCORD] ✅ Alert sent for {platform} ({post_url}) -> Status {res.status_code}")
    except Exception as e:
        print(f"   [DISCORD ERROR] {e}")


def get_tiktok_via_ytdlp() -> list:
    """Récupère les dernières vidéos TikTok via yt-dlp."""
    video_ids = []
    try:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--playlist-end", "6",
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


def get_insta_via_api() -> list:
    """Tente de récupérer les derniers posts Instagram via l'API web publique d'Instagram."""
    insta_posts = []  # Liste de tuples (shortcode, timestamp)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "X-IG-App-ID": "936619743392459",
            "Accept": "*/*",
            "Referer": INSTA_URL,
        }
        api_url = f"https://www.instagram.com/api/v1/users/web_profile_info/?username={INSTA_USER}"
        res = requests.get(api_url, headers=headers, timeout=15)
        if res.status_code == 200:
            data = res.json()
            edges = (
                data.get("data", {})
                .get("user", {})
                .get("edge_owner_to_timeline_media", {})
                .get("edges", [])
            )
            for edge in edges:
                node = edge.get("node", {})
                code = node.get("shortcode")
                ts = node.get("taken_at_timestamp", 0)
                if code:
                    insta_posts.append((code, int(ts)))
    except Exception as e:
        print(f"   [INFO] Direct IG API skipped: {e}")
    return insta_posts


async def scrape_socials_with_browser(need_tiktok: bool, need_insta: bool) -> tuple:
    tiktok_ids = []
    insta_posts = []

    if not need_tiktok and not need_insta:
        return tiktok_ids, insta_posts

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
                await page.goto(TIKTOK_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3500)
                html = await page.content()
                matches = re.findall(rf"/@{TIKTOK_USER}/video/(\d{{15,22}})", html)
                for m in matches:
                    if m not in tiktok_ids:
                        tiktok_ids.append(m)
            except Exception as e:
                print(f"   [!] TikTok browser error: {e}")

        if need_insta:
            try:
                await page.goto(INSTA_URL, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3500)
                html = await page.content()

                # Cherche shortcode + timestamp dans le JSON de la page
                codes = re.findall(r'"shortcode"\s*:\s*"([A-Za-z0-9_-]{8,15})"', html)
                for c in codes:
                    if c not in [x[0] for x in insta_posts]:
                        insta_posts.append((c, 0))

                if not insta_posts:
                    links = await page.locator("a[href*='/reel/'], a[href*='/p/']").all()
                    for link in links:
                        href = await link.get_attribute("href")
                        if href:
                            m = re.search(r"/(?:reel|p)/([A-Za-z0-9_-]+)/?", href)
                            if m and m.group(1) not in [x[0] for x in insta_posts]:
                                insta_posts.append((m.group(1), 0))
            except Exception as e:
                print(f"   [!] Instagram browser error: {e}")

        await browser.close()

    return tiktok_ids[:6], insta_posts[:6]


async def main():
    now_be = datetime.now(TZ)
    now_str = now_be.strftime("%H:%M")
    now_ts = int(time.time())
    print(f"=== START SILENT SOCIAL CHECK ({now_be.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    seen = load_seen()
    first_tiktok_run = not any(x.startswith("TT_") for x in seen)
    first_insta_run = not any(x.startswith("IG_") for x in seen)

    # 1. Récupération des vidéos/posts
    tt_ids = get_tiktok_via_ytdlp()
    ig_posts = get_insta_via_api()

    tt_browser, ig_browser = await scrape_socials_with_browser(
        need_tiktok=(len(tt_ids) == 0),
        need_insta=(len(ig_posts) == 0),
    )
    if not tt_ids:
        tt_ids = tt_browser
    if not ig_posts:
        ig_posts = ig_browser

    print(f"-> Checked TikTok ({len(tt_ids)} videos) & Instagram ({len(ig_posts)} posts)")

    # 2. Vérification TikTok (Uniquement si nouvelle vidéo sortie il y a moins d'une heure / 1h30)
    for vid in tt_ids:
        key = f"TT_{vid}"
        if key in seen:
            continue

        vid_ts = get_tiktok_timestamp(vid)
        age_seconds = now_ts - vid_ts if vid_ts > 0 else 0

        # Enregistre toujours dans la mémoire pour ne plus jamais la revérifier
        save_seen(key)
        seen.add(key)

        # N'alerte QUE si la vidéo est sortie il y a moins de MAX_AGE_SECONDS (1h30)
        if vid_ts > 0 and age_seconds <= MAX_AGE_SECONDS:
            post_url = f"https://www.tiktok.com/@{TIKTOK_USER}/video/{vid}"
            print(f"   🔥 NEW RECENT TIKTOK ({age_seconds // 60} min ago): {post_url}")
            send_discord_alert("TikTok", TIKTOK_USER, post_url, now_str)
        elif not first_tiktok_run and vid_ts == 0:
            post_url = f"https://www.tiktok.com/@{TIKTOK_USER}/video/{vid}"
            send_discord_alert("TikTok", TIKTOK_USER, post_url, now_str)

    # 3. Vérification Instagram
    for code, post_ts in ig_posts:
        key = f"IG_{code}"
        if key in seen:
            continue

        save_seen(key)
        seen.add(key)

        age_seconds = now_ts - post_ts if post_ts > 0 else 0

        # Si on a l'heure exacte du post IG : vérifie qu'il a moins de 1h30
        if post_ts > 0 and age_seconds <= MAX_AGE_SECONDS:
            post_url = f"https://www.instagram.com/p/{code}/"
            print(f"   🔥 NEW RECENT INSTAGRAM ({age_seconds // 60} min ago): {post_url}")
            send_discord_alert("Instagram", INSTA_USER, post_url, now_str)
        # Si pas d'heure mais que ce n'est pas le 1er lancement, c'est un nouveau post !
        elif not first_insta_run and post_ts == 0:
            post_url = f"https://www.instagram.com/p/{code}/"
            print(f"   🔥 NEW INSTAGRAM POST DETECTED: {post_url}")
            send_discord_alert("Instagram", INSTA_USER, post_url, now_str)

    print("=== CHECK COMPLETE (No spam, only real new posts!) ===")


if __name__ == "__main__":
    asyncio.run(main())

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
TIKTOK_USERS = [
    "tytyshi",
    "._leave.me_alone",
    "meyxdollw",
    "mahsa9946",
]

MAX_AGE_SECONDS = 10800
PING_USER_ID = "644995150774272020"
SEND_STATUS_EVERY_CHECK = False
MAX_DISCORD_FILE_MB = 9.5

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
    try:
        return int(video_id) >> 32
    except Exception:
        return 0


def post_to_discord(payload: dict) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_SOCIAL secret is missing!")
        return False
    try:
        res = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        print(f"   [DISCORD] Status {res.status_code}")
        return res.status_code in (200, 204)
    except Exception as e:
        print(f"   [DISCORD ERROR] {e}")
        return False


def has_video_stream(file_path: str) -> bool:
    """Vérifie si le fichier téléchargé contient une vraie piste vidéo (et pas juste le son d'un post photo)."""
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


def download_tiktok_video(post_url: str, video_id: str) -> str | None:
    """Télécharge la vidéo TikTok en MP4 (< 9.5 Mo). Retourne None si c'est un carrousel photo."""
    output_path = f"tiktok_{video_id}.mp4"
    if os.path.exists(output_path):
        os.remove(output_path)

    cmd = [
        "yt-dlp",
        "-f", "best[ext=mp4][vcodec^=avc][filesize<9.5M]/best[ext=mp4][filesize<9.5M]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "-o", output_path,
        post_url,
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=75)
        if os.path.exists(output_path):
            # Si pas de flux vidéo -> c'est un post photo, on supprime le faux .mp4 audio
            if not has_video_stream(output_path):
                print("   [SLIDESHOW DETECTED] Post contient des photos, pas une vidéo.")
                os.remove(output_path)
                return None

            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"   [DOWNLOAD] Video downloaded ({size_mb:.2f} MB)")
            if 0.05 < size_mb <= MAX_DISCORD_FILE_MB:
                return output_path
            else:
                os.remove(output_path)
    except Exception as e:
        print(f"   [!] Video download failed: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
    return None


def download_slideshow_images(post_url: str, video_id: str) -> list[str]:
    """Récupère et télécharge directement les photos d'un carrousel TikTok (via l'API TikWM)."""
    saved_files = []
    try:
        res = requests.get(
            "https://www.tikwm.com/api/",
            params={"url": post_url, "hd": 1},
            timeout=20
        )
        data = res.json().get("data", {})
        images = data.get("images", [])
        if not images:
            return []

        for idx, img_url in enumerate(images[:10]):  # Discord accepte jusqu'à 10 images par message
            img_res = requests.get(img_url, timeout=15)
            if img_res.status_code == 200:
                img_path = f"photo_{video_id}_{idx + 1}.jpg"
                with open(img_path, "wb") as f:
                    f.write(img_res.content)
                saved_files.append(img_path)
        print(f"   [SLIDESHOW] {len(saved_files)} photo(s) téléchargée(s)")
    except Exception as e:
        print(f"   [!] Slideshow download error: {e}")
    return saved_files


def send_video_alert(username: str, post_url: str, video_id: str, now_str: str) -> bool:
    """Envoie la vidéo OU les photos directement en fichiers sur Discord avec le nom du compte."""
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_SOCIAL secret is missing!")
        return False

    has_id = PING_USER_ID.isdigit()
    mention = f"<@{PING_USER_ID}>" if has_id else ""
    allowed_mentions = {"users": [PING_USER_ID] if has_id else []}

    # 1) Cas d'une vraie vidéo MP4
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

    # 2) Cas d'un carrousel photo (2 photos ou plus) -> envoi direct des images sur Discord
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

    # 3) Plan C (secours si vidéo > 9.5 Mo)
    embed_video_url = post_url.replace("www.tiktok.com", "tnktok.com")
    payload = {
        "username": "Alert Bot",
        "content": f"{mention} nouveau post de **@{username}**\n{embed_video_url}".strip(),
        "allowed_mentions": allowed_mentions,
    }
    return post_to_discord(payload)


def send_status(text: str, color: int, now_str: str) -> bool:
    payload = {
        "username": "Alert Bot",
        "embeds": [{
            "description": text,
            "color": color,
            "footer": {"text": f"Monitor • {now_str}"},
        }],
        "allowed_mentions": {"parse": []},
    }
    return post_to_discord(payload)


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
    now_str = now_be.strftime("%H:%M")
    now_ts = int(time.time())
    print(f"=== START TIKTOK CHECK ({now_be.strftime('%Y-%m-%d %H:%M:%S')}) ===")

    seen = load_seen()
    alerts_sent = 0
    checked = {}
    failed = []

    for user in TIKTOK_USERS:
        print(f"-> @{user}")
        tt_ids = get_tiktok_via_ytdlp(user)
        if not tt_ids:
            print("   yt-dlp returned nothing, trying browser...")
            tt_ids = await get_tiktok_via_browser(user)

        print(f"   {len(tt_ids)} posts read")
        if not tt_ids:
            failed.append(user)
            continue
        checked[user] = len(tt_ids)

        for vid in tt_ids:
            key = f"TT_{vid}"
            if key in seen:
                continue

            vid_ts = get_tiktok_timestamp(vid)
            age_seconds = now_ts - vid_ts if vid_ts > 0 else None
            post_url = f"https://www.tiktok.com/@{user}/video/{vid}"

            if age_seconds is not None and age_seconds <= MAX_AGE_SECONDS:
                print(f"   🔥 NEW TIKTOK ({age_seconds // 60} min ago): {post_url}")
                if send_video_alert(user, post_url, vid, now_str):
                    save_seen(key)
                    seen.add(key)
                    alerts_sent += 1
            else:
                save_seen(key)
                seen.add(key)

    if SEND_STATUS_EVERY_CHECK and (alerts_sent == 0 or failed):
        lines = []
        if alerts_sent == 0:
            lines.append(f"✅ Check done at {now_str} — **no new video**.")
        else:
            lines.append(f"✅ Check done at {now_str}.")
        for user, n in checked.items():
            lines.append(f"• @{user}: {n} posts checked")
        for user in failed:
            lines.append(f"⚠️ @{user}: **could not be read** (blocked or no response)")
        color = 16763904 if failed else 3447003
        send_status("\n".join(lines), color, now_str)

    print("=== CHECK COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())

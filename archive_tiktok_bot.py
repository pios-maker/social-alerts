import asyncio
import json
import os
import re
import subprocess
import time
import requests
from playwright.async_api import async_playwright
from playwright_stealth import Stealth

# ==========================================
# SETTINGS
# ==========================================
TARGET_USER = "apparentlysalma"       # TikTok username to archive (without @)
BATCH_SIZE = 30               # Max videos to download & upload per run (prevents timeouts/bans)
MAX_DISCORD_FILE_MB = 9.5

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_ARCHIVE")
MEMORY_FILE = f"archived_{TARGET_USER}.txt"


def load_archived() -> set:
    if not os.path.exists(MEMORY_FILE):
        return set()
    with open(MEMORY_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def save_archived(item_id: str):
    with open(MEMORY_FILE, "a", encoding="utf-8") as f:
        f.write(f"{item_id}\n")


def get_all_tiktok_ids_ytdlp(user: str) -> list:
    """Fetches all available video IDs from the user's profile."""
    video_ids = []
    try:
        cmd = [
            "yt-dlp",
            "--flat-playlist",
            "--print", "id",
            f"https://www.tiktok.com/@{user}",
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        for line in res.stdout.splitlines():
            clean = line.strip()
            if clean.isdigit() and len(clean) >= 15 and clean not in video_ids:
                video_ids.append(clean)
    except Exception as e:
        print(f"   [INFO] yt-dlp playlist fetch error for @{user}: {e}")
    return video_ids


async def get_tiktok_ids_browser_fallback(user: str) -> list:
    """Fallback: scrolls the profile in Playwright to collect post IDs."""
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
            await page.goto(f"https://www.tiktok.com/@{user}", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            for _ in range(8):
                await page.evaluate("window.scrollBy(0, 1500)")
                await page.wait_for_timeout(1500)
            html = await page.content()
            pattern = rf"/(?:@{re.escape(user)}/)?(?:video|photo)/(\d{{15,22}})"
            for m in re.findall(pattern, html):
                if m not in video_ids:
                    video_ids.append(m)
        except Exception as e:
            print(f"   [!] Browser fallback error: {e}")
        await browser.close()
    return video_ids


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
    """Compresses video with H.264/AAC so even longer TikToks stay under 9.5 MB."""
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


def download_video(post_url: str, video_id: str) -> str | None:
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

        print(f"   [COMPRESS] Video is {size_mb:.2f} MB, compressing for Discord...")
        if compress_to_fit_discord(raw_path, comp_path):
            os.remove(raw_path)
            new_size_mb = os.path.getsize(comp_path) / (1024 * 1024)
            if new_size_mb <= MAX_DISCORD_FILE_MB:
                return comp_path
            os.remove(comp_path)
    except Exception as e:
        print(f"   [!] Download error for {video_id}: {e}")
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
        print(f"   [!] Slideshow error: {e}")
    return saved_files


def upload_post_to_discord(user: str, post_url: str, video_id: str) -> bool:
    if not DISCORD_WEBHOOK_URL:
        print("❌ ERROR: DISCORD_WEBHOOK_ARCHIVE secret is missing!")
        return False

    # 1) Try video upload
    video_path = download_video(post_url, video_id)
    if video_path:
        try:
            payload = {
                "username": "TikTok Archiver",
                "content": f"`@{user}` • ID `{video_id}`",
                "allowed_mentions": {"parse": []},
            }
            with open(video_path, "rb") as f:
                res = requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"payload_json": json.dumps(payload)},
                    files={"file": (f"{user}_{video_id}.mp4", f, "video/mp4")},
                    timeout=60,
                )
            os.remove(video_path)
            if res.status_code in (200, 204):
                return True
        except Exception as e:
            print(f"   [UPLOAD ERROR] {e}")
            if os.path.exists(video_path):
                os.remove(video_path)

    # 2) Try slideshow photo upload
    photo_paths = download_slideshow_images(post_url, video_id)
    if photo_paths:
        open_files = []
        try:
            payload = {
                "username": "TikTok Archiver",
                "content": f"`@{user}` • ID `{video_id}` ({len(photo_paths)} photos)",
                "allowed_mentions": {"parse": []},
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
            print(f"   [PHOTO UPLOAD ERROR] {e}")
            for f_obj in open_files:
                try:
                    f_obj.close()
                except Exception:
                    pass
            for p in photo_paths:
                if os.path.exists(p):
                    os.remove(p)

    return False


async def main():
    print(f"=== ARCHIVING @{TARGET_USER} ===")
    archived = load_archived()

    all_ids = get_all_tiktok_ids_ytdlp(TARGET_USER)
    if not all_ids:
        print("yt-dlp returned nothing, trying browser scroll...")
        all_ids = await get_tiktok_ids_browser_fallback(TARGET_USER)

    print(f"Total posts discovered on profile: {len(all_ids)}")

    # Sort oldest-first (TikTok IDs increase chronologically)
    pending_ids = sorted([vid for vid in all_ids if vid not in archived], key=int)
    print(f"Remaining posts to archive: {len(pending_ids)}")

    batch = pending_ids[:BATCH_SIZE]
    uploaded_count = 0

    for vid in batch:
        post_url = f"https://www.tiktok.com/@{TARGET_USER}/video/{vid}"
        print(f"-> Processing {vid}...")
        if upload_post_to_discord(TARGET_USER, post_url, vid):
            save_archived(vid)
            archived.add(vid)
            uploaded_count += 1
            print(f"   ✅ Uploaded ({uploaded_count}/{len(batch)})")
            time.sleep(3)  # Pause to avoid Discord/TikTok rate limits
        else:
            print(f"   ⚠️ Failed/Skipped {vid}")

    print(f"=== BATCH COMPLETE: {uploaded_count} uploaded | {len(pending_ids) - uploaded_count} remaining ===")


if __name__ == "__main__":
    asyncio.run(main())

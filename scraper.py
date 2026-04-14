"""
PPV.to M3U Playlist Generator
Uses Playwright to intercept real .m3u8 URLs.
Falls back to iframe URL if extraction fails, so playlist is never empty.
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright


API_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE = "playlist.m3u"

# Wide window: everything that started today or ends in the next 48h
HOURS_BEFORE = 20   # catch streams that started earlier today
HOURS_AFTER  = 48   # catch upcoming streams up to 2 days ahead

EMBED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

M3U8_PATTERNS  = [".m3u8", "/mono.ts", "/tracks-v1a1"]
PLAYER_WAIT_MS = 10_000   # 10s — give slow players more time
MAX_CONCURRENT = 3


# ── helpers ───────────────────────────────────────────────────────────────────

def get_api_data():
    for url in API_MIRRORS:
        try:
            print(f"Trying {url} ...")
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    print(f"Got data from {url}")
                    return data
        except Exception as e:
            print(f"  FAIL {url}: {e}")
    return None


def format_time_label(ts):
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%m/%d %I:%M %p")


def in_window(starts_at, ends_at, now, win_start, win_end):
    if not starts_at:
        return False
    ev_start = datetime.fromtimestamp(starts_at, tz=timezone.utc)
    ev_end   = datetime.fromtimestamp(ends_at, tz=timezone.utc) if ends_at else None
    starts_in_range = win_start <= ev_start <= win_end
    currently_live  = ev_start <= now and (ev_end is None or ev_end >= now)
    return starts_in_range or currently_live


def fix_url(url):
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)


def get_origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


# ── Playwright extraction ─────────────────────────────────────────────────────

async def extract_m3u8(semaphore, browser, iframe_url):
    async with semaphore:
        found = []
        origin = get_origin(iframe_url)

        context = await browser.new_context(
            user_agent=EMBED_USER_AGENT,
            extra_http_headers={
                "Referer": iframe_url,
                "Origin":  origin,
            },
        )
        page = await context.new_page()

        def on_request(request):
            u = request.url
            if any(p in u for p in M3U8_PATTERNS) and u not in found:
                found.append(u)

        page.on("request", on_request)

        try:
            await page.goto(iframe_url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(PLAYER_WAIT_MS)
        except Exception as e:
            print(f"  page error: {e}")
        finally:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

        if found:
            result = fix_url(found[0])
            print(f"  [OK ] m3u8 found: {result[:80]}")
            return result

        print(f"  [---] no m3u8 found, will use iframe fallback")
        return None


# ── playlist writer ───────────────────────────────────────────────────────────

def write_playlist(entries):
    lines = ["#EXTM3U"]
    ok = fallback = 0

    for e in entries:
        stream_url = e.get("stream_url")
        iframe_url = e.get("iframe", "")
        origin     = get_origin(iframe_url) if iframe_url else ""

        # Always write an entry — use real m3u8 if found, else iframe as fallback
        if stream_url:
            final_url = stream_url
            ok += 1
        else:
            # Fallback: use the embed iframe URL
            # Some players (VLC, Kodi) can handle it; OTT may not, but
            # at least the playlist won't be empty.
            final_url = iframe_url
            fallback += 1

        time_label   = format_time_label(e["starts_at"]) if e["starts_at"] else "LIVE"
        display_name = f"{e['name']} {time_label}"
        logo         = e.get("poster", "")
        category     = e.get("category", "Sports")

        lines.append(
            f'#EXTINF:-1 tvg-logo="{logo}" group-title="{category}",{display_name}'
        )
        lines.append(f'#EXTVLCOPT:http-referrer={iframe_url}')
        lines.append(f'#EXTVLCOPT:http-origin={origin}')
        lines.append(f'#EXTVLCOPT:http-user-agent={EMBED_USER_AGENT}')
        lines.append(final_url)

    content = "\n".join(lines)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\nPlaylist saved -> {OUTPUT_FILE}")
    print(f"  {ok} real m3u8 streams")
    print(f"  {fallback} iframe fallbacks (no m3u8 intercepted)")
    print(f"  {ok + fallback} total entries")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now       = datetime.now(tz=timezone.utc)
    win_start = now - timedelta(hours=HOURS_BEFORE)
    win_end   = now + timedelta(hours=HOURS_AFTER)

    print(f"UTC now    : {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Window     : {win_start.strftime('%m/%d %H:%M')} -> {win_end.strftime('%m/%d %H:%M')} UTC")
    print("-" * 60)

    data = get_api_data()
    if not data:
        print("ERROR: all API mirrors failed.")
        return

    candidates = []

    for group in data.get("streams", []):
        category = group.get("category", "Unknown")
        if category == "24/7 Streams":
            continue

        for stream in group.get("streams", []):
            s_at = stream.get("starts_at", 0)
            e_at = stream.get("ends_at", 0)

            if not in_window(s_at, e_at, now, win_start, win_end):
                continue

            base = dict(
                category  = category,
                name      = stream.get("name", "Unknown"),
                poster    = stream.get("poster", ""),
                starts_at = s_at,
                iframe    = stream.get("iframe", ""),
            )
            candidates.append(base)

            for sub in stream.get("substreams", []):
                candidates.append({
                    **base,
                    "name"  : f"{stream['name']} ({sub.get('tag', 'Alt')})",
                    "iframe": sub.get("iframe", ""),
                })

    candidates.sort(key=lambda x: x["starts_at"])
    print(f"Streams in window : {len(candidates)}")
    print("-" * 60)

    if not candidates:
        print("No streams found in window — writing empty playlist.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        tasks   = [extract_m3u8(semaphore, browser, c["iframe"]) for c in candidates]
        results = await asyncio.gather(*tasks)

        await browser.close()

    for c, url in zip(candidates, results):
        c["stream_url"] = url

    write_playlist(candidates)


if __name__ == "__main__":
    asyncio.run(main())

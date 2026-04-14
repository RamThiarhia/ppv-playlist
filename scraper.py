"""
PPV.to M3U Playlist Generator
Uses Playwright to intercept real .m3u8 stream URLs from embed pages.
Referer/Origin per-stream = the embed page URL itself.
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta

import requests
from playwright.async_api import async_playwright


API_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE = "playlist.m3u"

HOURS_BEFORE = 2
HOURS_AFTER  = 24

EMBED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

M3U8_PATTERNS  = [".m3u8", "/mono.ts", "/tracks-v1a1"]
PLAYER_WAIT_MS = 8_000
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
    starts_soon    = win_start <= ev_start <= win_end
    currently_live = ev_start <= now and (ev_end is None or ev_end >= now)
    return starts_soon or currently_live


def fix_url(url):
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)


def embed_origin(iframe_url):
    """Extract just the scheme+host from the iframe URL, e.g. https://pooembed.eu"""
    from urllib.parse import urlparse
    p = urlparse(iframe_url)
    return f"{p.scheme}://{p.netloc}"


# ── Playwright extraction ─────────────────────────────────────────────────────

async def extract_m3u8(semaphore, browser, iframe_url):
    """
    Open embed page with:
      Referer : the iframe URL itself  (e.g. https://pooembed.eu/embed/...)
      Origin  : scheme+host of iframe  (e.g. https://pooembed.eu)
    Then intercept the .m3u8 network request.
    """
    async with semaphore:
        found = []
        origin = embed_origin(iframe_url)

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
            print(f"  page error ({iframe_url}): {e}")
        finally:
            await page.close()
            await context.close()

        if found:
            return fix_url(found[0])
        return None


# ── playlist writer ───────────────────────────────────────────────────────────

def write_playlist(entries):
    lines = ["#EXTM3U"]
    ok = fail = 0

    for e in entries:
        url        = e.get("stream_url")
        iframe_url = e.get("iframe", "")

        if not url:
            fail += 1
            continue

        time_label   = format_time_label(e["starts_at"]) if e["starts_at"] else "LIVE"
        display_name = f"{e['name']} {time_label}"
        logo         = e.get("poster", "")
        category     = e.get("category", "Sports")
        origin       = embed_origin(iframe_url)

        lines.append(
            f'#EXTINF:-1 tvg-logo="{logo}" group-title="{category}",{display_name}'
        )
        # Referer = the embed page URL, Origin = its host — matches what the
        # CDN expects based on how the player loaded the stream.
        lines.append(f'#EXTVLCOPT:http-referrer={iframe_url}')
        lines.append(f'#EXTVLCOPT:http-origin={origin}')
        lines.append(f'#EXTVLCOPT:http-user-agent={EMBED_USER_AGENT}')
        lines.append(url)
        ok += 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nPlaylist saved -> {OUTPUT_FILE}  ({ok} ok, {fail} failed)")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now       = datetime.now(tz=timezone.utc)
    win_start = now - timedelta(hours=HOURS_BEFORE)
    win_end   = now + timedelta(hours=HOURS_AFTER)

    print(f"UTC now : {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Window  : {win_start.strftime('%H:%M')} -> {win_end.strftime('%H:%M')} UTC")
    print("-" * 56)

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
            )

            candidates.append({**base, "iframe": stream["iframe"]})

            for sub in stream.get("substreams", []):
                candidates.append({
                    **base,
                    "name"  : f"{stream['name']} ({sub.get('tag','Alt')})",
                    "iframe": sub["iframe"],
                })

    candidates.sort(key=lambda x: x["starts_at"])
    print(f"Streams in window : {len(candidates)}")
    print("-" * 56)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        tasks   = [extract_m3u8(semaphore, browser, c["iframe"]) for c in candidates]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for c, url in zip(candidates, results):
        c["stream_url"] = url
        time_s = format_time_label(c["starts_at"]) if c["starts_at"] else "LIVE"
        status = f"OK  {url[:70]}" if url else "FAIL"
        print(f"  [{c['category']}] {c['name']} @ {time_s} -> {status}")

    write_playlist(candidates)


if __name__ == "__main__":
    asyncio.run(main())

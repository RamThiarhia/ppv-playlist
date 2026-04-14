"""
PPV.to M3U Playlist Generator
Extracts real .m3u8 URLs by intercepting network requests via Playwright.
Uses the iframe URL with #player=clappr#autoplay=true appended.
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

OUTPUT_FILE    = "playlist.m3u"
HOURS_BEFORE   = 20
HOURS_AFTER    = 48
PLAYER_WAIT_MS = 12_000
MAX_CONCURRENT = 2   # lower = more stable on GitHub Actions

EMBED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Match any URL that looks like a video stream
STREAM_PATTERNS = [
    r"\.m3u8",
    r"/tracks-v1a1/",
    r"/mono\.ts",
    r"\.ts\.m3u8",
]


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
    in_range = win_start <= ev_start <= win_end
    is_live  = ev_start <= now and (ev_end is None or ev_end >= now)
    return in_range or is_live


def fix_url(url):
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)


def get_origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def is_stream_url(url):
    return any(re.search(p, url, re.I) for p in STREAM_PATTERNS)


async def extract_m3u8(semaphore, browser, iframe_url):
    """
    Strategy:
    1. Open embed page, intercept ALL network requests
    2. Also intercept responses and scan JS for stream URLs
    3. After page load, try clicking the player to trigger autoplay
    4. Also evaluate JS to pull stream URL from player config
    """
    async with semaphore:
        found = []
        # Append clappr autoplay flags like the original code does
        load_url = iframe_url
        if "#" not in load_url:
            load_url = f"{iframe_url}#player=clappr#autoplay=true"

        origin = get_origin(iframe_url)

        context = await browser.new_context(
            user_agent=EMBED_USER_AGENT,
            extra_http_headers={
                "Referer": iframe_url,
                "Origin":  origin,
            },
        )

        # Block ads/trackers to speed things up
        await context.route(
            re.compile(r"(google-analytics|doubleclick|googlesyndication|adservice)"),
            lambda route: route.abort()
        )

        page = await context.new_page()

        # Intercept all requests
        def on_request(request):
            u = request.url
            if is_stream_url(u) and u not in found:
                print(f"    >> intercepted request: {u[:100]}")
                found.append(u)

        # Also scan response bodies for m3u8 URLs embedded in JS/JSON
        async def on_response(response):
            u = response.url
            ct = response.headers.get("content-type", "")
            if any(x in ct for x in ["javascript", "json", "text"]):
                try:
                    body = await response.text()
                    # Look for m3u8 URLs inside JS/JSON responses
                    matches = re.findall(r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*', body)
                    for m in matches:
                        if m not in found:
                            print(f"    >> found in response body: {m[:100]}")
                            found.append(m)
                except Exception:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        try:
            await page.goto(load_url, wait_until="domcontentloaded", timeout=20_000)

            # Wait for initial JS to run
            await page.wait_for_timeout(3000)

            # Try clicking anywhere on the player to trigger autoplay
            try:
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(2000)
            except Exception:
                pass

            # Try clicking a play button if visible
            for selector in [
                "button[class*='play']",
                "div[class*='play']",
                ".vjs-big-play-button",
                ".play-button",
                "[aria-label*='Play']",
                "video",
            ]:
                try:
                    el = page.locator(selector).first
                    if await el.is_visible(timeout=1000):
                        await el.click()
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            # Try extracting from JS player objects
            for js_expr in [
                # Clappr
                "window.player && window.player.options && window.player.options.source",
                "window.player && window.player._options && window.player._options.source",
                # JWPlayer
                "window.jwplayer && window.jwplayer().getPlaylistItem && window.jwplayer().getPlaylistItem().file",
                # VideoJS
                "window.videojs && document.querySelector('video') && document.querySelector('video').src",
                # Generic
                "document.querySelector('video') && document.querySelector('video').src",
                "document.querySelector('source') && document.querySelector('source').src",
            ]:
                try:
                    result = await page.evaluate(js_expr)
                    if result and isinstance(result, str) and is_stream_url(result):
                        if result not in found:
                            print(f"    >> found via JS eval: {result[:100]}")
                            found.append(result)
                        break
                except Exception:
                    pass

            # Final wait for any late-loading streams
            await page.wait_for_timeout(PLAYER_WAIT_MS - 5000)

        except Exception as e:
            print(f"  page error ({iframe_url}): {e}")
        finally:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

        if found:
            result = fix_url(found[0])
            print(f"  [OK ] {result[:90]}")
            return result

        print(f"  [---] no stream found for: {iframe_url}")
        return None


def write_playlist(entries):
    lines = ["#EXTM3U"]
    ok = fallback = 0

    for e in entries:
        stream_url = e.get("stream_url")
        iframe_url = e.get("iframe", "")
        origin     = get_origin(iframe_url) if iframe_url else ""

        if stream_url:
            final_url = stream_url
            ok += 1
        else:
            final_url = iframe_url   # fallback — keeps playlist non-empty
            fallback += 1

        time_label   = format_time_label(e["starts_at"]) if e["starts_at"] else "LIVE"
        display_name = f"{e['name']} {time_label}"

        lines.append(
            f'#EXTINF:-1 tvg-logo="{e.get("poster","")}" '
            f'group-title="{e.get("category","Sports")}",{display_name}'
        )
        lines.append(f'#EXTVLCOPT:http-referrer={iframe_url}')
        lines.append(f'#EXTVLCOPT:http-origin={origin}')
        lines.append(f'#EXTVLCOPT:http-user-agent={EMBED_USER_AGENT}')
        lines.append(final_url)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSaved {OUTPUT_FILE}: {ok} m3u8 + {fallback} iframe fallback = {ok+fallback} total")


async def main():
    now       = datetime.now(tz=timezone.utc)
    win_start = now - timedelta(hours=HOURS_BEFORE)
    win_end   = now + timedelta(hours=HOURS_AFTER)

    print(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Window: {win_start.strftime('%m/%d %H:%M')} -> {win_end.strftime('%m/%d %H:%M')}")
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
                    "name"  : f"{stream['name']} ({sub.get('tag','Alt')})",
                    "iframe": sub.get("iframe", ""),
                })

    candidates.sort(key=lambda x: x["starts_at"])
    print(f"Streams in window: {len(candidates)}")
    print("-" * 60)

    if not candidates:
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        print("No streams found.")
        return

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--autoplay-policy=no-user-gesture-required"],
        )
        tasks   = [extract_m3u8(semaphore, browser, c["iframe"]) for c in candidates]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for c, url in zip(candidates, results):
        c["stream_url"] = url

    write_playlist(candidates)


if __name__ == "__main__":
    asyncio.run(main())

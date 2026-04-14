"""
Sports M3U Playlist Generator
- Events + stream URLs : CDN Live TV API (free, no token protection)
- TV logos             : PPV.to API (matched by sport/event name)
- Output               : playlist.m3u playable in OTT Navigator / Exoplayer
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
from playwright.async_api import async_playwright


# ── API config ────────────────────────────────────────────────────────────────

CDNTV_API   = "https://api.cdnlivetv.tv/api/v1/events/sports/?user=cdnlivetv&plan=free"
CDNTV_USER  = "cdnlivetv"
CDNTV_PLAN  = "free"

PPV_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE    = "playlist.m3u"
HOURS_BEFORE   = 2
HOURS_AFTER    = 24
PLAYER_WAIT_MS = 10_000
MAX_CONCURRENT = 3

EMBED_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

M3U8_PATTERNS = [r"\.m3u8", r"/tracks-v1a1/", r"/mono\.ts"]


# ── fetch CDN Live TV events ──────────────────────────────────────────────────

def get_cdntv_events():
    """
    Returns list of dicts:
      category, name, player_url, logo (from ppv), starts_at (datetime)
    """
    try:
        print("Fetching CDN Live TV events ...")
        r = requests.get(CDNTV_API, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  FAIL: {e}")
        return []

    # API returns either 'cdn-live-tv' or 'cdnlivetv.tv' as root key
    sports_data = data.get("cdn-live-tv") or data.get("cdnlivetv.tv") or {}

    now       = datetime.now(tz=timezone.utc)
    win_start = now - timedelta(hours=HOURS_BEFORE)
    win_end   = now + timedelta(hours=HOURS_AFTER)

    events = []

    for sport, event_list in sports_data.items():
        # Skip metadata keys like total_events, total_events_soccer etc.
        if not isinstance(event_list, list):
            continue

        for ev in event_list:
            home = ev.get("homeTeam", "")
            away = ev.get("awayTeam", "")
            if not (home and away):
                continue

            name       = f"{away} vs {home}"
            tournament = ev.get("tournament", sport)
            status     = ev.get("status", "")
            start_str  = ev.get("start", "")

            # Parse start time (format: "2025-11-21 15:30")
            try:
                starts_at = datetime.strptime(start_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except Exception:
                starts_at = now  # fallback: treat as now

            # Filter: only live or upcoming within window
            end_str = ev.get("end", "")
            try:
                ends_at = datetime.strptime(end_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except Exception:
                ends_at = starts_at + timedelta(hours=3)

            in_range = win_start <= starts_at <= win_end
            is_live  = status == "live" or (starts_at <= now <= ends_at)

            if not (in_range or is_live):
                continue

            channels = ev.get("channels", [])
            if not channels:
                continue

            # Use first available channel player URL
            player_url = channels[0].get("url", "")
            channel_img = channels[0].get("image", "")

            if not player_url:
                continue

            events.append({
                "category"  : tournament,
                "name"      : name,
                "player_url": player_url,
                "channel_img": channel_img,
                "starts_at" : starts_at,
                "status"    : status,
                # logo will be filled from PPV.to later
                "logo"      : channel_img,
            })

    print(f"  Found {len(events)} events in window")
    return events


# ── fetch PPV.to logos ────────────────────────────────────────────────────────

def get_ppv_logos():
    """Build a dict of event_name -> logo_url from PPV.to API."""
    logos = {}
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to logos from {url} ...")
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    for group in data.get("streams", []):
                        for stream in group.get("streams", []):
                            name   = stream.get("name", "").lower().strip()
                            poster = stream.get("poster", "")
                            if name and poster:
                                logos[name] = poster
                    print(f"  Loaded {len(logos)} logos from PPV.to")
                    return logos
        except Exception as e:
            print(f"  FAIL {url}: {e}")
    print("  Could not load PPV.to logos — will use CDN channel images")
    return logos


def match_logo(event_name, ppv_logos):
    """Fuzzy match event name against PPV.to logos."""
    name_lower = event_name.lower().strip()

    # Exact match
    if name_lower in ppv_logos:
        return ppv_logos[name_lower]

    # Partial match — check if all words of one are in the other
    name_words = set(re.sub(r"[^a-z0-9 ]", "", name_lower).split())
    for ppv_name, logo in ppv_logos.items():
        ppv_words = set(re.sub(r"[^a-z0-9 ]", "", ppv_name).split())
        # If 70%+ of words overlap, consider it a match
        if name_words and ppv_words:
            overlap = len(name_words & ppv_words) / max(len(name_words), len(ppv_words))
            if overlap >= 0.7:
                return logo

    return None


# ── Playwright: extract m3u8 from CDN Live TV player page ────────────────────

def get_origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def is_stream_url(url):
    return any(re.search(p, url, re.I) for p in M3U8_PATTERNS)


def fix_url(url):
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)


async def extract_m3u8(semaphore, browser, player_url):
    async with semaphore:
        found  = []
        origin = get_origin(player_url)

        context = await browser.new_context(
            user_agent=EMBED_USER_AGENT,
            extra_http_headers={
                "Referer": player_url,
                "Origin" : origin,
            },
        )

        # Block ads/trackers
        await context.route(
            re.compile(r"(google-analytics|doubleclick|googlesyndication|adservice)"),
            lambda route: route.abort()
        )

        page = await context.new_page()

        def on_request(request):
            u = request.url
            if is_stream_url(u) and u not in found:
                print(f"    >> intercepted: {u[:90]}")
                found.append(u)

        async def on_response(response):
            ct = response.headers.get("content-type", "")
            if any(x in ct for x in ["javascript", "json", "text"]):
                try:
                    body = await response.text()
                    for m in re.findall(r'https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*', body):
                        if m not in found:
                            print(f"    >> in response body: {m[:90]}")
                            found.append(m)
                except Exception:
                    pass

        page.on("request",  on_request)
        page.on("response", on_response)

        try:
            await page.goto(player_url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(3000)

            # Click to trigger autoplay
            try:
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(2000)
            except Exception:
                pass

            # Try common play button selectors
            for sel in [
                "button[class*='play']", "div[class*='play']",
                ".vjs-big-play-button", ".play-button",
                "[aria-label*='Play']", "video",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=800):
                        await el.click()
                        await page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            # JS extraction from known player objects
            for expr in [
                "window.player && window.player.options && window.player.options.source",
                "window.player && window.player._options && window.player._options.source",
                "window.jwplayer && window.jwplayer().getPlaylistItem && window.jwplayer().getPlaylistItem().file",
                "document.querySelector('video') && document.querySelector('video').src",
                "document.querySelector('source') && document.querySelector('source').src",
            ]:
                try:
                    val = await page.evaluate(expr)
                    if val and isinstance(val, str) and is_stream_url(val) and val not in found:
                        print(f"    >> JS eval: {val[:90]}")
                        found.append(val)
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(PLAYER_WAIT_MS - 5000)

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
            print(f"  [OK ] {result[:90]}")
            return result

        print(f"  [---] no stream found: {player_url}")
        return None


# ── playlist writer ───────────────────────────────────────────────────────────

def format_time(dt):
    return dt.strftime("%m/%d %I:%M %p")


def write_playlist(entries):
    lines = ["#EXTM3U"]
    ok = fallback = 0

    for e in entries:
        stream_url = e.get("stream_url")
        player_url = e.get("player_url", "")
        origin     = get_origin(player_url) if player_url else ""
        logo       = e.get("logo", "")
        category   = e.get("category", "Sports")
        time_label = format_time(e["starts_at"])
        name       = f"{e['name']} {time_label}"

        if stream_url:
            final_url = stream_url
            ok += 1
        else:
            final_url = player_url  # fallback
            fallback += 1

        lines.append(
            f'#EXTINF:-1 tvg-logo="{logo}" group-title="{category}",{name}'
        )
        lines.append(f'#EXTVLCOPT:http-referrer={player_url}')
        lines.append(f'#EXTVLCOPT:http-origin={origin}')
        lines.append(f'#EXTVLCOPT:http-user-agent={EMBED_USER_AGENT}')
        lines.append(final_url)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSaved {OUTPUT_FILE}: {ok} m3u8 | {fallback} fallback | {ok+fallback} total")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now = datetime.now(tz=timezone.utc)
    print(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # 1. Get events from CDN Live TV
    events = get_cdntv_events()
    if not events:
        print("No events found — writing empty playlist.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    # 2. Get logos from PPV.to and match them
    ppv_logos = get_ppv_logos()
    for ev in events:
        matched = match_logo(ev["name"], ppv_logos)
        if matched:
            ev["logo"] = matched  # override channel img with PPV.to poster

    print("=" * 60)
    print(f"Extracting streams for {len(events)} events ...")

    # 3. Extract real m3u8 URLs via Playwright
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        tasks   = [extract_m3u8(semaphore, browser, ev["player_url"]) for ev in events]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for ev, url in zip(events, results):
        ev["stream_url"] = url

    # 4. Write playlist
    write_playlist(events)


if __name__ == "__main__":
    asyncio.run(main())

"""
Sports M3U Playlist Generator
- Events : CDN Live TV API (free)
- Logos  : PPV.to API  
- Streams: extracted via Playwright by intercepting network requests
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, urlencode

import requests
from playwright.async_api import async_playwright


# ── config ────────────────────────────────────────────────────────────────────

CDNTV_SPORTS_API  = "https://api.cdnlivetv.tv/api/v1/events/sports/?user=cdnlivetv&plan=free"
CDNTV_CHANNEL_API = "https://api.cdnlivetv.tv/api/v1/channels/?user=cdnlivetv&plan=free"

PPV_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE    = "playlist.m3u"
HOURS_BEFORE   = 2
HOURS_AFTER    = 24
PLAYER_WAIT_MS = 12_000
MAX_CONCURRENT = 2

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

# Patterns that identify a real video stream URL
STREAM_RE = re.compile(r"\.m3u8|/tracks-v1a1/|/mono\.ts|\.ts\b", re.I)


# ── helpers ───────────────────────────────────────────────────────────────────

def get_origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def fix_url(url):
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)


def format_time(dt):
    return dt.strftime("%m/%d %I:%M %p")


def is_stream(url):
    return bool(STREAM_RE.search(url))


# ── Step 1: get events from CDN Live TV ──────────────────────────────────────

def get_cdntv_events():
    print("Fetching CDN Live TV sports events ...")
    try:
        r = requests.get(CDNTV_SPORTS_API, headers=HEADERS, timeout=15)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        print(f"  FAIL: {e}")
        return []

    sports_data = raw.get("cdn-live-tv") or raw.get("cdnlivetv.tv") or {}

    now       = datetime.now(tz=timezone.utc)
    win_start = now - timedelta(hours=HOURS_BEFORE)
    win_end   = now + timedelta(hours=HOURS_AFTER)

    events = []
    for sport, event_list in sports_data.items():
        if not isinstance(event_list, list):
            continue
        for ev in event_list:
            home = ev.get("homeTeam", "")
            away = ev.get("awayTeam", "")
            if not (home and away):
                continue

            status    = ev.get("status", "")
            start_str = ev.get("start", "")
            end_str   = ev.get("end", "")

            try:
                starts_at = datetime.strptime(start_str, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except Exception:
                starts_at = now

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

            for ch in channels:
                player_url = ch.get("url", "")
                if not player_url:
                    continue
                events.append({
                    "category"  : ev.get("tournament", sport),
                    "name"      : f"{away} vs {home}",
                    "player_url": player_url,
                    "channel"   : ch.get("channel_name", ""),
                    "ch_logo"   : ch.get("image", ""),
                    "starts_at" : starts_at,
                    "status"    : status,
                    "logo"      : ch.get("image", ""),  # overwritten later by PPV.to
                })

    print(f"  {len(events)} events in window")
    return events


# ── Step 2: match PPV.to logos ────────────────────────────────────────────────

def get_ppv_logos():
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to logos from {url} ...")
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code == 200 and r.json().get("success"):
                logos = {}
                for group in r.json().get("streams", []):
                    for stream in group.get("streams", []):
                        key    = re.sub(r"[^a-z0-9]", "", stream.get("name","").lower())
                        poster = stream.get("poster", "")
                        if key and poster:
                            logos[key] = poster
                print(f"  {len(logos)} logos loaded")
                return logos
        except Exception as e:
            print(f"  FAIL: {e}")
    return {}


def match_logo(event_name, ppv_logos):
    key = re.sub(r"[^a-z0-9]", "", event_name.lower())
    if key in ppv_logos:
        return ppv_logos[key]
    # sliding window: try matching any 60%+ overlap
    words = set(re.sub(r"[^a-z0-9 ]","", event_name.lower()).split())
    best_score, best_logo = 0, None
    for ppv_key, logo in ppv_logos.items():
        ppv_words = set(re.sub(r"[^a-z0-9 ]","", ppv_key).split())
        if not words or not ppv_words:
            continue
        overlap = len(words & ppv_words) / max(len(words), len(ppv_words))
        if overlap > best_score:
            best_score, best_logo = overlap, logo
    return best_logo if best_score >= 0.6 else None


# ── Step 3: Playwright extraction ────────────────────────────────────────────

async def extract_stream(semaphore, browser, player_url):
    """
    Load the CDN Live TV player page in a real browser.
    Intercept every network request and grab the first .m3u8 URL.
    Also try direct requests to common stream endpoint patterns.
    """
    async with semaphore:
        found = []
        origin = get_origin(player_url)

        # --- Strategy A: try common direct stream URL patterns first (fast) ---
        # CDN Live TV often exposes stream at /stream/ endpoint directly
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(player_url)
        qs     = parse_qs(parsed.query)
        name   = qs.get("name", [""])[0]
        code   = qs.get("code", [""])[0]
        user   = qs.get("user", ["cdnlivetv"])[0]
        plan   = qs.get("plan", ["free"])[0]

        if name and code:
            candidates = [
                f"https://api.cdnlivetv.tv/api/v1/channels/stream/?name={name}&code={code}&user={user}&plan={plan}",
                f"https://cdnlivetv.tv/api/v1/channels/stream/?name={name}&code={code}&user={user}&plan={plan}",
                f"https://api.cdnlivetv.tv/api/v1/channels/hls/?name={name}&code={code}&user={user}&plan={plan}",
            ]
            for candidate in candidates:
                try:
                    resp = requests.get(
                        candidate,
                        headers={**HEADERS, "Referer": player_url, "Origin": origin},
                        timeout=8,
                        allow_redirects=True,
                    )
                    ct = resp.headers.get("content-type", "")
                    final_url = resp.url  # follow redirects

                    if is_stream(final_url):
                        print(f"    >> direct URL: {final_url[:90]}")
                        return fix_url(final_url)

                    if "mpegurl" in ct or "m3u" in ct:
                        print(f"    >> direct stream content-type: {ct}")
                        return fix_url(final_url)

                    if resp.status_code == 200 and "#EXTM3U" in resp.text[:50]:
                        print(f"    >> direct m3u8 content: {final_url[:90]}")
                        return fix_url(final_url)

                except Exception:
                    pass

        # --- Strategy B: Playwright browser interception ---
        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            extra_http_headers={
                "Referer": player_url,
                "Origin" : origin,
            },
        )

        await context.route(
            re.compile(r"(google-analytics|doubleclick|googlesyndication|adservice|facebook\.net)"),
            lambda route: route.abort()
        )

        page = await context.new_page()

        def on_request(request):
            u = request.url
            if is_stream(u) and u not in found:
                print(f"    >> intercepted: {u[:90]}")
                found.append(u)

        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                u  = response.url
                # Direct m3u8 response
                if "mpegurl" in ct or "m3u" in ct:
                    if u not in found:
                        print(f"    >> m3u8 response: {u[:90]}")
                        found.append(u)
                    return
                # Scan JS/JSON for embedded stream URLs
                if any(x in ct for x in ["javascript", "json", "text/plain"]):
                    body = await response.text()
                    for m in re.findall(r'https?://[^\s\'"<>\\]+\.m3u8[^\s\'"<>\\]*', body):
                        if m not in found:
                            print(f"    >> in JS body: {m[:90]}")
                            found.append(m)
            except Exception:
                pass

        page.on("request",  on_request)
        page.on("response", on_response)

        try:
            await page.goto(player_url, wait_until="domcontentloaded", timeout=20_000)
            await page.wait_for_timeout(3_000)

            # Click centre of page to trigger autoplay
            try:
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(2_000)
            except Exception:
                pass

            # Click known play button selectors
            for sel in [
                ".vjs-big-play-button", "button[class*='play']",
                "div[class*='play']", "[aria-label*='Play']",
                ".play-button", "video",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=800):
                        await el.click()
                        await page.wait_for_timeout(2_000)
                        if found:
                            break
                except Exception:
                    pass

            # Pull stream URL from JS player objects
            js_exprs = [
                "window.player?.options?.source",
                "window.player?._options?.source",
                "window.jwplayer?.()?.getPlaylistItem?.()?.file",
                "window.Clappr?.player?.options?.source",
                "document.querySelector('video')?.src",
                "document.querySelector('source')?.src",
                # look for any variable holding an m3u8
                """(()=>{
                    const scripts = [...document.querySelectorAll('script')].map(s=>s.innerText).join(' ');
                    const m = scripts.match(/https?:\\/\\/[^\\s'"<>]+\\.m3u8[^\\s'"<>]*/);
                    return m ? m[0] : null;
                })()""",
            ]
            for expr in js_exprs:
                try:
                    val = await page.evaluate(expr)
                    if val and isinstance(val, str) and is_stream(val) and val not in found:
                        print(f"    >> JS eval: {val[:90]}")
                        found.append(val)
                        break
                except Exception:
                    pass

            # Final wait
            if not found:
                await page.wait_for_timeout(PLAYER_WAIT_MS - 5_000)

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

        print(f"  [---] no stream: {player_url}")
        return None


# ── Step 4: write playlist ────────────────────────────────────────────────────

def write_playlist(entries):
    lines = ["#EXTM3U"]
    ok = fallback = skipped = 0

    for e in entries:
        stream_url = e.get("stream_url")
        player_url = e.get("player_url", "")
        logo       = e.get("logo", "")
        category   = e.get("category", "Sports")
        time_label = format_time(e["starts_at"])
        channel    = e.get("channel", "")
        name       = f"{e['name']} {time_label}"
        if channel:
            name += f" [{channel}]"

        if stream_url:
            final_url = stream_url
            ok += 1
        elif player_url:
            # Only include fallback if it looks like a direct URL, not an HTML page
            if player_url.endswith(".m3u8") or is_stream(player_url):
                final_url = player_url
                fallback += 1
            else:
                # Skip — HTML player page will break Exoplayer
                skipped += 1
                continue
        else:
            skipped += 1
            continue

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{category}",{name}')
        lines.append(final_url)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSaved {OUTPUT_FILE}:")
    print(f"  {ok} real m3u8 streams")
    print(f"  {fallback} direct fallbacks")
    print(f"  {skipped} skipped (no stream found)")
    print(f"  {ok + fallback} total playable entries")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now = datetime.now(tz=timezone.utc)
    print(f"UTC: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    events = get_cdntv_events()
    if not events:
        print("No events found.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    ppv_logos = get_ppv_logos()
    for ev in events:
        logo = match_logo(ev["name"], ppv_logos)
        if logo:
            ev["logo"] = logo

    print("=" * 60)
    print(f"Extracting streams for {len(events)} events ...")
    print("-" * 60)

    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-web-security",
            ],
        )
        tasks   = [extract_stream(semaphore, browser, ev["player_url"]) for ev in events]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for ev, url in zip(events, results):
        ev["stream_url"] = url

    write_playlist(events)


if __name__ == "__main__":
    asyncio.run(main())

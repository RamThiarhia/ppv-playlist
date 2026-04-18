"""
NBA M3U Playlist Generator
- Events + streams : roxiestreams.su/nba
- Names/logos/times: PPV.to API (matched by sport + time window)
- Times displayed  : Philippine Time (UTC+8)
- Headers preserved: Referer, Origin, User-Agent (required by CDN)
"""

import asyncio
import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError
from selectolax.parser import HTMLParser


# ── config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://roxiestreams.su"
NBA_URL  = urljoin(BASE_URL, "nba")

PPV_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE       = "playlist.m3u"
MAX_CONCURRENT    = 1       # ONE at a time — no race conditions
PAGE_TIMEOUT      = 30_000  # 30s page load timeout
CLAPPR_TIMEOUT    = 15_000  # 15s wait for clappr to init
BUTTON_TIMEOUT    = 5_000   # 5s for stream button
TIME_MATCH_WINDOW = 90      # minutes for time-based PPV matching

PHT = timezone(timedelta(hours=8))
ET  = timezone(timedelta(hours=-4))  # US Eastern (EDT)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def get_origin(url):
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def fix_url(url):
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)

def fmt_time_pht(dt):
    if dt is None:
        return ""
    return dt.astimezone(PHT).strftime("%m/%d %I:%M %p PHT")

def normalize(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

def slug_to_words(url):
    path  = urlparse(url).path
    slug  = path.rstrip("/").split("/")[-1]
    slug  = re.sub(r"-\d+$", "", slug)
    return set(slug.lower().split("-")) - {"vs", "at", ""}


# ── Step 1: scrape roxiestreams NBA ──────────────────────────────────────────

def get_roxie_events():
    print(f"Scraping {NBA_URL} ...")
    events = []

    try:
        r    = requests.get(NBA_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
        soup = HTMLParser(r.content)

        rows = soup.css("table#eventsTable tbody tr")
        if not rows:
            rows = soup.css("table tbody tr")

        print(f"  Rows found: {len(rows)}")

        for row in rows:
            cells = row.css("td")
            a     = row.css_first("td a")
            if not a:
                continue

            event_name = a.text(strip=True)
            href       = a.attributes.get("href", "")
            if not href:
                continue

            time_str = ""
            for cell in cells:
                txt = cell.text(strip=True)
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            events.append({
                "roxie_name"    : event_name,
                "link"          : urljoin(BASE_URL, href),
                "roxie_time_str": time_str,
            })
            print(f"  Found: '{event_name}' @ '{time_str}'")

    except Exception as e:
        print(f"  FAIL: {e}")

    print(f"  Total: {len(events)} NBA events")
    return events


# ── Step 2: get PPV.to NBA streams ───────────────────────────────────────────

def get_ppv_nba():
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to from {url} ...")
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                continue
            data = r.json()
            if not data.get("success"):
                continue

            nba_streams = []
            for group in data.get("streams", []):
                cat = group.get("category", "")
                if cat.lower() not in ("basketball", "nba"):
                    continue
                for s in group.get("streams", []):
                    ts = s.get("starts_at", 0)
                    if not ts:
                        continue
                    entry = {
                        "name"     : s.get("name", ""),
                        "poster"   : s.get("poster", ""),
                        "starts_at": datetime.fromtimestamp(ts, tz=timezone.utc),
                    }
                    nba_streams.append(entry)
                    print(f"  PPV: '{entry['name']}' @ {fmt_time_pht(entry['starts_at'])}")

            print(f"  Total: {len(nba_streams)} PPV NBA streams")
            return nba_streams

        except Exception as e:
            print(f"  FAIL {url}: {e}")

    return []


# ── Step 3: match roxie → PPV ────────────────────────────────────────────────

def match_event_to_ppv(roxie_ev, ppv_streams):
    roxie_name = roxie_ev.get("roxie_name", "")
    roxie_link = roxie_ev.get("link", "")
    time_str   = roxie_ev.get("roxie_time_str", "")

    # Strategy 1 — name word overlap
    rwords = set(normalize(roxie_name).split())
    best_score, best_stream = 0, None
    for s in ppv_streams:
        pwords  = set(normalize(s["name"]).split())
        if not rwords or not pwords:
            continue
        overlap = len(rwords & pwords) / max(len(rwords), len(pwords))
        if overlap > best_score:
            best_score, best_stream = overlap, s
    if best_score >= 0.5:
        print(f"  [name {best_score:.0%}] '{roxie_name}' -> '{best_stream['name']}'")
        return best_stream

    # Strategy 2 — URL slug word match
    slug_words = slug_to_words(roxie_link)
    if slug_words:
        best_slug_score, best_slug_stream = 0, None
        for s in ppv_streams:
            pwords  = set(normalize(s["name"]).split())
            overlap = len(slug_words & pwords) / max(len(slug_words), len(pwords)) if pwords else 0
            if overlap > best_slug_score:
                best_slug_score, best_slug_stream = overlap, s
        if best_slug_score >= 0.4:
            print(f"  [slug {best_slug_score:.0%}] '{roxie_name}' -> '{best_slug_stream['name']}'")
            return best_slug_stream

    # Strategy 3 — time window match
    time_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)
    if time_match:
        hour   = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm   = time_match.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        now_et     = datetime.now(tz=ET)
        candidates = []
        for day_offset in (0, 1, -1):
            try:
                dt_et = now_et.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                ) + timedelta(days=day_offset)
                candidates.append(dt_et.astimezone(timezone.utc))
            except Exception:
                pass

        window = timedelta(minutes=TIME_MATCH_WINDOW)
        for s in ppv_streams:
            for candidate in candidates:
                if abs(s["starts_at"] - candidate) <= window:
                    print(f"  [time] '{roxie_name}' -> '{s['name']}'")
                    return s

    print(f"  [no match] '{roxie_name}'")
    return None


# ── Step 4: Playwright — extract ONE stream at a time ────────────────────────

async def extract_stream(browser, event, index, total):
    """
    Processes a single event page sequentially.
    No semaphore needed since we run one at a time.
    """
    link   = event["link"]
    origin = get_origin(link)
    name   = event["roxie_name"]

    print(f"\n[{index}/{total}] Processing: {name}")
    print(f"  URL: {link}")

    context = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Referer": link, "Origin": origin},
    )
    page       = await context.new_page()
    stream_url = None

    try:
        print(f"  Loading page ...")
        resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

        if not resp or resp.status != 200:
            print(f"  HTTP {resp.status if resp else 'none'} — skipping")
            return None

        print(f"  Page loaded (HTTP {resp.status})")

        # Wait a moment for JS to initialise
        await page.wait_for_timeout(2_000)

        # Click the stream button
        try:
            btn = page.locator("button.streambutton").first
            if await btn.is_visible(timeout=BUTTON_TIMEOUT):
                print(f"  Clicking stream button ...")
                await btn.click(force=True, click_count=2, timeout=BUTTON_TIMEOUT)
                await page.wait_for_timeout(1_000)
            else:
                print(f"  No stream button — clicking page centre")
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(1_000)
        except Exception as e:
            print(f"  Click error: {e}")

        # Wait for clapprPlayer to be defined
        print(f"  Waiting for clapprPlayer (up to {CLAPPR_TIMEOUT//1000}s) ...")
        try:
            await page.wait_for_function(
                "() => typeof clapprPlayer !== 'undefined'",
                timeout=CLAPPR_TIMEOUT,
            )
            stream_url = await page.evaluate("() => clapprPlayer.options.source")
            print(f"  clapprPlayer source: {stream_url}")
        except PWTimeoutError:
            print(f"  clapprPlayer not found — trying fallbacks ...")

        # Fallback JS player objects
        if not stream_url:
            for expr in [
                "window.player?.options?.source",
                "window.jwplayer?.()?.getPlaylistItem?.()?.file",
                "document.querySelector('video')?.src",
                "document.querySelector('source')?.src",
            ]:
                try:
                    val = await page.evaluate(expr)
                    if val and isinstance(val, str) and ".m3u8" in val:
                        stream_url = val
                        print(f"  Fallback found: {val}")
                        break
                except Exception:
                    pass

    except Exception as e:
        print(f"  Error: {e}")
    finally:
        try:
            await page.close()
            await context.close()
        except Exception:
            pass

    if stream_url:
        stream_url = fix_url(stream_url)
        print(f"  [OK] -> {stream_url[:90]}")
    else:
        print(f"  [FAIL] no stream URL found")

    return stream_url


# ── Step 5: write playlist ────────────────────────────────────────────────────

def write_playlist(entries):
    lines   = ["#EXTM3U"]
    ok      = 0
    skipped = 0

    for e in entries:
        url = e.get("stream_url")
        if not url:
            skipped += 1
            continue

        link   = e.get("link", "")
        origin = get_origin(link) if link else ""
        logo   = e.get("logo", "")
        name   = e.get("display_name", e.get("roxie_name", "Unknown"))

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="NBA",{name}')
        lines.append(f'#EXTVLCOPT:http-referrer={link}')
        lines.append(f'#EXTVLCOPT:http-origin={origin}')
        lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        lines.append(url)
        ok += 1

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSaved {OUTPUT_FILE}: {ok} streams, {skipped} skipped")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now_utc = datetime.now(tz=timezone.utc)
    now_pht = now_utc.astimezone(PHT)
    print(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PHT: {now_pht.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    print("=" * 60)

    # 1. Scrape roxie
    roxie_events = get_roxie_events()
    if not roxie_events:
        print("No NBA events found on roxiestreams.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    # 2. Get PPV NBA streams
    print("-" * 60)
    ppv_streams = get_ppv_nba()

    # 3. Match events
    print("-" * 60)
    print("Matching events to PPV.to ...")
    for ev in roxie_events:
        ppv = match_event_to_ppv(ev, ppv_streams)
        if ppv:
            ev["display_name"] = f"{ppv['name']} {fmt_time_pht(ppv.get('starts_at'))}".strip()
            ev["logo"]         = ppv.get("poster", "")
            ev["starts_at"]    = ppv.get("starts_at")
        else:
            ev["display_name"] = ev["roxie_name"]
            ev["logo"]         = ""
            ev["starts_at"]    = now_utc

    # Sort by start time
    roxie_events.sort(key=lambda x: x.get("starts_at") or now_utc)

    # 4. Extract streams ONE BY ONE — no concurrency
    print("=" * 60)
    print(f"Extracting streams for {len(roxie_events)} events (sequential) ...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )

        total = len(roxie_events)
        for i, ev in enumerate(roxie_events, start=1):
            url = await extract_stream(browser, ev, i, total)
            ev["stream_url"] = url

        await browser.close()

    # 5. Write playlist
    print("\n" + "=" * 60)
    write_playlist(roxie_events)


if __name__ == "__main__":
    asyncio.run(main())

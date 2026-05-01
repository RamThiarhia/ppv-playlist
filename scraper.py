"""
NBA M3U Playlist Generator — FIXED
Fixes:
  1. Playwright used for roxie scraping (JS-rendered table)
  2. PPV time-matching anchored to roxie date, not just "now"
  3. CLAPPR timeout extended + network request interception as primary method
  4. Detailed diagnostics so failures are visible
"""

import asyncio
import re
import json
import sys
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, urlparse

import requests
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

# ── config ────────────────────────────────────────────────────────────────────

BASE_URL = "https://istreameast.app"
NBA_URL  = BASE_URL


PPV_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE       = "playlist.m3u"
SCHEDULE_FILE     = "schedule.json"
PAGE_TIMEOUT      = 30_000   # 30s page load
CLAPPR_TIMEOUT    = 25_000   # extended: 25s (was 15s)
BUTTON_TIMEOUT    = 5_000
TIME_MATCH_WINDOW = 90       # minutes

PHT = timezone(timedelta(hours=8))
ET  = timezone(timedelta(hours=-4))  # EDT; change to -5 in winter (EST)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ── helpers ───────────────────────────────────────────────────────────────────

def get_origin(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"

def fix_url(url: str) -> str:
    return re.sub(r"index\.m3u8$", "tracks-v1a1/mono.ts.m3u8", url, flags=re.I)

def fmt_time_pht(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(PHT).strftime("%m/%d %I:%M %p PHT")

def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

def slug_to_words(url: str) -> set:
    path  = urlparse(url).path
    slug  = path.rstrip("/").split("/")[-1]
    slug  = re.sub(r"-\d+$", "", slug)
    return set(slug.lower().split("-")) - {"vs", "at", ""}


def load_schedule() -> list:
    try:
        with open(SCHEDULE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for item in data:
                # Convert ISO string back to datetime
                if "starts_at_iso" in item and item["starts_at_iso"]:
                    item["starts_at"] = datetime.fromisoformat(item["starts_at_iso"])
                else:
                    item["starts_at"] = None
            print(f"Loaded {len(data)} scheduled events from {SCHEDULE_FILE}")
            return data
    except Exception as e:
        print(f"No valid {SCHEDULE_FILE} found or error loading: {e}")
        return []


def save_schedule(schedule: list):
    export_data = []
    for item in schedule:
        # Create a shallow copy to dict without mutating original datetime
        row = dict(item)
        if "starts_at" in row and isinstance(row["starts_at"], datetime):
            row["starts_at_iso"] = row["starts_at"].isoformat()
            del row["starts_at"]
        export_data.append(row)
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(export_data, f, indent=2)
    print(f"Saved {len(export_data)} events state to {SCHEDULE_FILE}")


# ── Step 1: scrape roxiestreams NBA via Playwright ───────────────────────────
# FIX #1: Use Playwright instead of requests so JS-rendered tables are visible.

async def get_istreameast_events(browser) -> list:
    print(f"Scraping {BASE_URL} with Playwright ...")
    events  = []
    context = await browser.new_context(user_agent=USER_AGENT)
    page    = await context.new_page()

    try:
        await page.goto(BASE_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        # Give any late JS a moment
        await page.wait_for_timeout(2_000)

        # Dump raw HTML for diagnosis
        html = await page.content()
        with open("istreameast_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("  Saved istreameast_debug.html for inspection")

        # New selectors based on example
        items = await page.query_selector_all("li.f1-podium--item")
        print(f"  Items found: {len(items)}")

        for item in items:
            # Check for live status
            time_elem = await item.query_selector(".SaatZamanBilgisi")
            if time_elem:
                time_text = (await time_elem.inner_text()).strip().lower()
                if "live" not in time_text:
                    continue
            else:
                continue

            # Sport check
            rank_elem = await item.query_selector(".f1-podium--rank")
            if not rank_elem:
                continue
            sport = (await rank_elem.inner_text()).strip().upper()
            
            # STRICT NBA FILTER (Excludes WNBA, MLB, etc.)
            if sport != "NBA":
                continue

            # Event name
            driver_elem = await item.query_selector(".f1-podium--driver")
            if not driver_elem:
                continue
            
            event_name = (await driver_elem.inner_text()).strip()
            # Try to get the more descriptive name if span exists
            inner_span = await driver_elem.query_selector("span.d-md-inline")
            if inner_span:
                event_name = (await inner_span.inner_text()).strip()

            # Link
            link_elem = await item.query_selector("a.f1-podium--link")
            if not link_elem:
                continue
            href = await link_elem.get_attribute("href")
            if not href:
                continue
            
            full_link = urljoin(BASE_URL, href)
            events.append({
                "roxie_name"    : event_name, 
                "link"          : full_link,
                "roxie_time_str": "LIVE",
            })
            print(f"  Found NBA: '{event_name}' -> {full_link}")

    except Exception as e:
        print(f"  FAIL scraping istreameast: {e}")
    finally:
        await page.close()
        await context.close()

    print(f"  Total: {len(events)} NBA events")
    return events



# ── Step 2: get PPV.to NBA streams ───────────────────────────────────────────

def get_ppv_nba() -> list:
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to from {url} ...")
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code} — trying next mirror")
                continue
            data = r.json()
            if not data.get("success"):
                print(f"  API returned success=false — trying next mirror")
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

                    uri_name = s.get("uri_name", "").lower()
                    # Filter: must be in NBA category OR have nba/ prefix in uri_name
                    # AND must not be WNBA
                    if "wnba" in uri_name:
                        continue
                    
                    is_nba = (cat.lower() == "nba") or uri_name.startswith("nba/")
                    if not is_nba:
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

    print("  WARNING: All PPV mirrors failed — names/logos will be missing")
    return []


# ── Step 3: match roxie → PPV ────────────────────────────────────────────────
# FIX #2: Time matching now anchors to today's PHT date, not a raw "now + offsets"
#         to avoid drift when the script runs near midnight or after a delay.

def parse_roxie_time(time_str: str, reference_utc: datetime):
    """
    Convert a time string from Roxie to UTC.
    The user confirms Roxie uses UTC.
    We ignore the 'April 28' part if it conflicts, as the links are confirmed current.
    """
    # Extract just the time part: HH:MM AM/PM
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)
    if not m:
        return []

    hour   = int(m.group(1))
    minute = int(m.group(2))
    ampm   = m.group(3).upper()

    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0

    # Assume UTC as per user
    ref_utc = reference_utc
    candidates = []
    # Try today, tomorrow, and yesterday in UTC to find the best match for the time
    for day_offset in (0, 1, -1):
        try:
            dt_utc = ref_utc.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ) + timedelta(days=day_offset)
            candidates.append(dt_utc.replace(tzinfo=timezone.utc))
        except Exception:
            pass
    return candidates


def match_event_to_ppv(roxie_ev: dict, ppv_streams: list, reference_utc: datetime):
    roxie_name = roxie_ev.get("roxie_name", "")
    roxie_link = roxie_ev.get("link", "")
    time_str   = roxie_ev.get("roxie_time_str", "")

    # Strategy 1 — name word overlap
    rwords = set(normalize(roxie_name).split())
    best_score, best_stream = 0.0, None
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
        best_slug_score, best_slug_stream = 0.0, None
        for s in ppv_streams:
            pwords  = set(normalize(s["name"]).split())
            overlap = len(slug_words & pwords) / max(len(slug_words), len(pwords)) if pwords else 0
            if overlap > best_slug_score:
                best_slug_score, best_slug_stream = overlap, s
        if best_slug_score >= 0.4:
            print(f"  [slug {best_slug_score:.0%}] '{roxie_name}' -> '{best_slug_stream['name']}'")
            return best_slug_stream

    # Strategy 3 — time window match (FIX: uses reference_utc, not live now())
    candidates = parse_roxie_time(time_str, reference_utc)
    window     = timedelta(minutes=TIME_MATCH_WINDOW)
    for s in ppv_streams:
        for candidate in candidates:
            if abs(s["starts_at"] - candidate) <= window:
                print(f"  [time] '{roxie_name}' -> '{s['name']}'")
                return s

    print(f"  [no match] '{roxie_name}' (time_str='{time_str}')")
    return None


# ── Step 4: Playwright — extract stream URL ───────────────────────────────────
# FIX #3: Use network request interception as PRIMARY method.
#         clapprPlayer JS eval is now the fallback, not the primary.

async def extract_stream(browser, event: dict, index: int, total: int):
    link   = event["link"]
    origin = get_origin(link)
    name   = event["roxie_name"]

    print(f"\n[{index}/{total}] Processing: {name}")
    print(f"  URL: {link}")

    context = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Referer": NBA_URL, "Origin": origin},
    )
    page       = await context.new_page()
    stream_url = None

    # --- PRIMARY: intercept network requests for .m3u8 URLs ---
    intercepted_urls: list[str] = []

    def on_request(req):
        url = req.url
        if ".m3u8" in url:
            intercepted_urls.append(url)

    page.on("request", on_request)

    try:
        print(f"  Loading page ...")
        resp = await page.goto(link, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        if not resp or resp.status != 200:
            print(f"  HTTP {resp.status if resp else 'none'} — skipping")
            return None

        print(f"  Page loaded (HTTP {resp.status})")
        # Wait for buttons to potentially render via JS
        await page.wait_for_timeout(4_000)

        # 1. Look for iframe#wp_player and its src
        iframe_elem = await page.query_selector("iframe#wp_player")
        if iframe_elem:
            iframe_src = await iframe_elem.get_attribute("src")
            if iframe_src:
                print(f"  Found iframe#wp_player: {iframe_src}")
                # Optional: try to fetch iframe src content directly if needed
                try:
                    # Use a new page to load the iframe src to extract the source regex
                    iframe_page = await context.new_page()
                    await iframe_page.goto(iframe_src, wait_until="networkidle", timeout=PAGE_TIMEOUT)
                    iframe_content = await iframe_page.content()
                    pattern = re.compile(r'const\s+source\s+=\s+"([^"]*)"', re.I)
                    if match := pattern.search(iframe_content):
                        stream_url = match.group(1)
                        print(f"  [regex match] {stream_url[:90]}")
                    await iframe_page.close()
                except Exception as e:
                    print(f"  Iframe regex extraction failed: {e}")

        if not stream_url:
            # 2. Click stream button (if regex failed)
            try:
                button_selector = "button.streambutton, button, a.btn, .stream-link, [class*='stream']"
                buttons = await page.locator(button_selector).all()
                
                target_btn = None
                if buttons:
                    print(f"  Found {len(buttons)} potential stream buttons")
                    # Priority 1: "Stream 2"
                    for b in buttons:
                        txt = (await b.inner_text()).strip().lower()
                        if "stream 2" in txt:
                            target_btn = b
                            print(f"  Found priority: {txt}")
                            break
                    
                    # Priority 2: "Stream 1"
                    if not target_btn:
                        for b in buttons:
                            txt = (await b.inner_text()).strip().lower()
                            if "stream 1" in txt:
                                target_btn = b
                                print(f"  Falling back to: {txt}")
                                break
                    
                    # Priority 3: First available button
                    if not target_btn:
                        target_btn = buttons[0]
                        print(f"  Using first available button")

                    if target_btn:
                        print(f"  Clicking button ...")
                        await target_btn.click(force=True, timeout=5000)
                        await page.wait_for_timeout(2_000)
                else:
                    print(f"  No buttons found — clicking page centre")
                    await page.mouse.click(640, 360)
                    await page.wait_for_timeout(2_000)
            except Exception as e:
                print(f"  Button click failed/skipped: {e}")

        # 2. Check intercepted first
        await page.wait_for_timeout(3_000)
        if intercepted_urls:
            stream_url = intercepted_urls[-1]
            print(f"  [intercept] {stream_url[:90]}")
        else:
            # Fallback: wait for clapprPlayer
            print(f"  No intercepted .m3u8 — waiting for clapprPlayer ...")
            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined' && clapprPlayer.options && clapprPlayer.options.source",
                    timeout=10000,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
                print(f"  [clapprPlayer] {stream_url}")
            except Exception:
                pass

        # 3. JS fallbacks
        if not stream_url:
            for expr in [
                "window.player?.options?.source",
                "document.querySelector('video source')?.src",
                "document.querySelector('video')?.src",
            ]:
                try:
                    val = await page.evaluate(expr)
                    if val and isinstance(val, str) and ".m3u8" in val:
                        stream_url = val
                        print(f"  [JS fallback] {val[:90]}")
                        break
                except Exception:
                    pass

    except Exception as e:
        print(f"  Main extraction error: {e}")
    finally:
        page.remove_listener("request", on_request)
        try:
            await page.close()
            await context.close()
        except Exception:
            pass

    if stream_url:
        stream_url = fix_url(stream_url)
        print(f"  [OK] -> {stream_url[:90]}")
    else:
        print(f"  [FAIL] no stream URL found for '{name}'")

    return stream_url


# ── Step 5: write playlist ────────────────────────────────────────────────────

def write_playlist(entries: list):
    lines   = ["#EXTM3U"]
    ok      = 0

    for e in entries:
        url = e.get("stream_url") or "http://no-stream-yet.com/dummy.m3u8"

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

    print(f"\nSaved {OUTPUT_FILE}: {ok} streams written in total")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    # Snapshot "now" ONCE — used as the stable reference throughout
    now_utc = datetime.now(tz=timezone.utc)
    now_pht = now_utc.astimezone(PHT)
    print(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PHT: {now_pht.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    
    is_full_fetch = (now_pht.hour == 0) or ("--full" in sys.argv)
    print(f"MODE: {'FULL FETCH (12:00 AM PHT)' if is_full_fetch else 'UPDATE (Roxie streams only)'}")
    print("=" * 60)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )

        schedule_state = []
        if is_full_fetch:
            print("1. [FULL FETCH] Getting PPV NBA streams ...")
            ppv_streams = get_ppv_nba()
            if not ppv_streams:
                print("No games found on PPV.to. Saving empty schedule.")
                save_schedule([])
                write_playlist([])
                await browser.close()
                return
            
            # Initialize schedule state with fresh PPV data
            schedule_state = ppv_streams
        else:
            print("1. [UPDATE] Loading schedule.json ...")
            schedule_state = load_schedule()
            if not schedule_state:
                print("No active schedule found in schedule.json. Generating empty playlist.")
                write_playlist([])
                await browser.close()
                return

        # 2. Scrape istreameast
        print("-" * 60)
        roxie_events = await get_istreameast_events(browser)


        # 3. Match roxie events -> schedule_state items
        print("-" * 60)
        print("Matching roxie events to schedule items ...")
        for ev in roxie_events:
            ppv = match_event_to_ppv(ev, schedule_state, reference_utc=now_utc)
            if ppv:
                # Store the latest Roxie metadata/link on the matched schedule item
                ppv["roxie_name"] = ev["roxie_name"]
                ppv["link"] = ev["link"]

        # Sort schedule by start time
        schedule_state.sort(key=lambda x: x.get("starts_at") or now_utc)

        # 4. Extract streams sequentially for matched items
        print("=" * 60)
        to_extract = [s for s in schedule_state if s.get("link")]
        print(f"Extracting streams for {len(to_extract)} scheduled events (out of {len(schedule_state)} total) ...")

        for i, s in enumerate(to_extract, start=1):
            url = await extract_stream(browser, s, i, len(to_extract))
            if url:
                s["stream_url"] = url
            # If `url` is None (fails extraction or timeout), we intentionally DO NOT overwrite `s.get("stream_url")`
            # This maintains whatever stream_url was previously saved as the fallback.

        # Prepare formatting for the final playlist output
        for s in schedule_state:
            time_str = fmt_time_pht(s.get('starts_at')).strip()
            if time_str and time_str in s['name']:
                s["display_name"] = s['name']
            else:
                s["display_name"] = f"{s['name']} {time_str}".strip()
            s["logo"] = s.get("poster", "")

        await browser.close()

    # 5. Write playlist & final schedule state (with new extracted URLs or preserved fallbacks!)
    print("\n" + "=" * 60)
    save_schedule(schedule_state)
    write_playlist(schedule_state)


if __name__ == "__main__":
    asyncio.run(main())

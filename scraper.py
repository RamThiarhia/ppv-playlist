"""
NBA M3U Playlist Generator — FIXED v2
Fixes:
  1. Playwright used for roxie scraping (JS-rendered table)
  2. PPV time-matching anchored to roxie date, not just "now"
  3. CLAPPR timeout extended + network request interception as primary method
  4. Detailed diagnostics so failures are visible
  5. [NEW] Multiple stream links per game — prioritize stream link 2 over stream link 1
  6. [NEW] Stream validation to detect broken/looping streams
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

BASE_URL = "https://roxiestreams.su"
NBA_URL  = urljoin(BASE_URL, "nba")

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

# Stream validation: how many seconds of segments to check before declaring broken
STREAM_VALIDATE_TIMEOUT = 12  # seconds

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
# FIX #5: Collect ALL stream links per game row (stream 1, stream 2, etc.)

async def get_roxie_events_async(browser) -> list:
    print(f"Scraping {NBA_URL} with Playwright (JS-rendered) ...")
    events  = []
    context = await browser.new_context(user_agent=USER_AGENT)
    page    = await context.new_page()

    try:
        await page.goto(NBA_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2_000)

        # Dump raw HTML for diagnosis
        html = await page.content()
        with open("roxie_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("  Saved roxie_debug.html for inspection")

        # Try both known selectors
        rows = await page.query_selector_all("table#eventsTable tbody tr")
        if not rows:
            rows = await page.query_selector_all("table tbody tr")
        if not rows:
            rows = await page.query_selector_all("tr:has(a)")

        print(f"  Rows found: {len(rows)}")

        for row in rows:
            # ── Collect ALL links in this row ──────────────────────────────
            all_anchors = await row.query_selector_all("a")
            if not all_anchors:
                continue

            # Build list of (label_text, href) for every anchor in this row
            stream_links = []
            event_name   = ""
            for anchor in all_anchors:
                href = await anchor.get_attribute("href")
                if not href:
                    continue
                text = (await anchor.inner_text()).strip()
                full_link = urljoin(BASE_URL, href)
                stream_links.append({"label": text, "link": full_link})
                # Use first anchor text as the event name (usually the game title)
                if not event_name:
                    event_name = text

            if not stream_links:
                continue

            # Extract time string from cells
            cells    = await row.query_selector_all("td")
            time_str = ""
            for cell in cells:
                txt = (await cell.inner_text()).strip()
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            # ── Determine primary/fallback links ───────────────────────────
            # Strategy: if there are 2+ links, stream link 2 (index 1) is preferred.
            # Roxie typically has: [Stream 1 link, Stream 2 link] per row.
            # We store link_primary = stream 2 (preferred), link_fallback = stream 1.
            if len(stream_links) >= 2:
                link_primary  = stream_links[1]["link"]   # Stream 2 — prioritized
                link_fallback = stream_links[0]["link"]   # Stream 1 — fallback
                print(f"  Found (2 streams): '{event_name}' @ '{time_str}'")
                print(f"    Primary  (stream 2): {link_primary}")
                print(f"    Fallback (stream 1): {link_fallback}")
            else:
                link_primary  = stream_links[0]["link"]   # Only stream available
                link_fallback = None
                print(f"  Found (1 stream): '{event_name}' @ '{time_str}' -> {link_primary}")

            events.append({
                "roxie_name"    : event_name,
                "link"          : link_primary,   # PRIMARY link used for extraction
                "link_fallback" : link_fallback,  # FALLBACK link if primary fails/loops
                "all_links"     : [s["link"] for s in stream_links],
                "roxie_time_str": time_str,
            })

    except Exception as e:
        print(f"  FAIL scraping roxie: {e}")
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

def parse_roxie_time(time_str: str, reference_utc: datetime):
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

    ref_et     = reference_utc.astimezone(ET)
    candidates = []
    for day_offset in (0, 1, -1):
        try:
            dt_et = ref_et.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ) + timedelta(days=day_offset)
            candidates.append(dt_et.astimezone(timezone.utc))
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

    # Strategy 3 — time window match
    candidates = parse_roxie_time(time_str, reference_utc)
    window     = timedelta(minutes=TIME_MATCH_WINDOW)
    for s in ppv_streams:
        for candidate in candidates:
            if abs(s["starts_at"] - candidate) <= window:
                print(f"  [time] '{roxie_name}' -> '{s['name']}'")
                return s

    print(f"  [no match] '{roxie_name}' (time_str='{time_str}')")
    return None


# ── Step 4a: validate a stream URL (detect broken/looping) ───────────────────
# FIX #6: Fetch the .m3u8 manifest and check that it has real segments and
#         that the segments are actually reachable (HTTP 200). A looping/broken
#         stream often has duplicate segment filenames or returns HTTP errors.

async def validate_stream_url(stream_url: str, referer: str = "") -> bool:
    """
    Returns True if the stream looks healthy, False if broken/looping.
    Checks:
      - M3U8 manifest is reachable (HTTP 200)
      - Manifest contains at least 1 media segment (.ts / .aac / .mp4)
      - First segment is reachable (HTTP 200)
      - No obvious looping (all segment names identical)
    """
    if not stream_url or "dummy" in stream_url:
        return False

    headers = {"User-Agent": USER_AGENT}
    if referer:
        headers["Referer"] = referer
        headers["Origin"]  = get_origin(referer)

    try:
        # 1. Fetch the manifest
        r = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.get(stream_url, headers=headers, timeout=STREAM_VALIDATE_TIMEOUT)
        )
        if r.status_code != 200:
            print(f"    [validate] manifest HTTP {r.status_code} — FAIL")
            return False

        content = r.text

        # 2. Extract segment lines (non-comment, non-empty)
        segments = [
            line.strip() for line in content.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        if not segments:
            print(f"    [validate] manifest has no segments — FAIL")
            return False

        # 3. Detect looping: all segment names are identical
        unique_segs = set(segments)
        if len(unique_segs) == 1 and len(segments) > 2:
            print(f"    [validate] all {len(segments)} segments identical (looping) — FAIL")
            return False

        # 4. Check the first segment is reachable
        first_seg = segments[0]
        if not first_seg.startswith("http"):
            # Resolve relative URL
            base = stream_url.rsplit("/", 1)[0]
            first_seg = f"{base}/{first_seg}"

        seg_r = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.head(first_seg, headers=headers, timeout=STREAM_VALIDATE_TIMEOUT, allow_redirects=True)
        )
        if seg_r.status_code not in (200, 206):
            print(f"    [validate] first segment HTTP {seg_r.status_code} — FAIL")
            return False

        print(f"    [validate] {len(segments)} segments, unique={len(unique_segs)} — OK")
        return True

    except Exception as e:
        print(f"    [validate] exception: {e} — FAIL")
        return False


# ── Step 4b: Playwright — extract stream URL from a page ─────────────────────

async def _extract_from_page(browser, link: str) -> str | None:
    """Extract a .m3u8 stream URL from a single Roxie stream page."""
    origin = get_origin(link)

    context = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Referer": link, "Origin": origin},
    )
    page           = await context.new_page()
    stream_url     = None
    intercepted_urls: list[str] = []

    def on_request(req):
        url = req.url
        if ".m3u8" in url:
            intercepted_urls.append(url)

    page.on("request", on_request)

    try:
        resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

        if not resp or resp.status != 200:
            print(f"    HTTP {resp.status if resp else 'none'} — skipping page")
            return None

        await page.wait_for_timeout(2_000)

        # Click stream button
        try:
            btn = page.locator("button.streambutton").first
            if await btn.is_visible(timeout=BUTTON_TIMEOUT):
                await btn.click(force=True, timeout=BUTTON_TIMEOUT)
                await page.wait_for_timeout(1_500)
            else:
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(1_500)
        except Exception as e:
            print(f"    Click warning: {e}")

        await page.wait_for_timeout(3_000)

        if intercepted_urls:
            # Prefer index.m3u8 entries; fall back to last intercepted
            index_urls = [u for u in intercepted_urls if "index.m3u8" in u]
            stream_url = (index_urls or intercepted_urls)[-1]
            print(f"    [intercept] {stream_url[:90]}")
        else:
            print(f"    No intercepted .m3u8 — waiting for clapprPlayer ({CLAPPR_TIMEOUT//1000}s) ...")
            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined' && clapprPlayer.options && clapprPlayer.options.source",
                    timeout=CLAPPR_TIMEOUT,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
                print(f"    [clapprPlayer] {stream_url}")
            except PWTimeoutError:
                print(f"    clapprPlayer timed out — trying JS fallbacks ...")

        if not stream_url:
            for expr in [
                "window.player?.options?.source",
                "window.jwplayer?.()?.getPlaylistItem?.()?.file",
                "document.querySelector('video source')?.src",
                "document.querySelector('video')?.src",
            ]:
                try:
                    val = await page.evaluate(expr)
                    if val and isinstance(val, str) and ".m3u8" in val:
                        stream_url = val
                        print(f"    [JS fallback] {val[:90]}")
                        break
                except Exception:
                    pass

        if not stream_url and intercepted_urls:
            stream_url = intercepted_urls[-1]
            print(f"    [late intercept] {stream_url[:90]}")

    except Exception as e:
        print(f"    Error loading page: {e}")
    finally:
        page.remove_listener("request", on_request)
        try:
            await page.close()
            await context.close()
        except Exception:
            pass

    if stream_url:
        stream_url = fix_url(stream_url)

    return stream_url


# ── Step 4c: extract with fallback logic ─────────────────────────────────────
# FIX #5: Try primary link (stream 2) first, validate it.
#         If invalid/broken, try fallback link (stream 1).

async def extract_stream(browser, event: dict, index: int, total: int):
    name          = event["roxie_name"]
    link_primary  = event.get("link")           # Stream 2 (prioritized)
    link_fallback = event.get("link_fallback")  # Stream 1 (fallback)

    print(f"\n[{index}/{total}] Processing: {name}")

    # ── Try PRIMARY link (Stream 2) ──────────────────────────────────────────
    stream_url = None
    if link_primary:
        print(f"  Trying PRIMARY (stream 2): {link_primary}")
        raw = await _extract_from_page(browser, link_primary)
        if raw:
            print(f"  Validating stream 2 URL ...")
            ok = await validate_stream_url(raw, referer=link_primary)
            if ok:
                stream_url = raw
                print(f"  [OK] Stream 2 is healthy — using it.")
            else:
                print(f"  [WARN] Stream 2 failed validation — trying stream 1 fallback ...")
        else:
            print(f"  [WARN] Stream 2 extraction failed — trying stream 1 fallback ...")

    # ── Try FALLBACK link (Stream 1) if primary failed ───────────────────────
    if not stream_url and link_fallback:
        print(f"  Trying FALLBACK (stream 1): {link_fallback}")
        raw = await _extract_from_page(browser, link_fallback)
        if raw:
            print(f"  Validating stream 1 URL ...")
            ok = await validate_stream_url(raw, referer=link_fallback)
            if ok:
                stream_url = raw
                print(f"  [OK] Stream 1 is healthy — using it.")
                # Update the event link so the playlist referrer is correct
                event["link"] = link_fallback
            else:
                print(f"  [WARN] Stream 1 also failed validation — saving raw URL as last resort.")
                stream_url = raw  # save anyway; something > nothing
                event["link"] = link_fallback
        else:
            print(f"  [FAIL] Both streams failed extraction for '{name}'")

    if stream_url:
        print(f"  Final URL: {stream_url[:90]}")
    else:
        print(f"  [FAIL] No stream URL found for '{name}'")

    return stream_url


# ── Step 5: write playlist ────────────────────────────────────────────────────

def write_playlist(entries: list):
    lines = ["#EXTM3U"]
    ok    = 0

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
            schedule_state = ppv_streams
        else:
            print("1. [UPDATE] Loading schedule.json ...")
            schedule_state = load_schedule()
            if not schedule_state:
                print("No active schedule found in schedule.json. Generating empty playlist.")
                write_playlist([])
                await browser.close()
                return

        # 2. Scrape roxie (JS-rendered, uses Playwright)
        print("-" * 60)
        roxie_events = await get_roxie_events_async(browser)

        # 3. Match roxie events -> schedule_state items
        print("-" * 60)
        print("Matching roxie events to schedule items ...")
        for ev in roxie_events:
            ppv = match_event_to_ppv(ev, schedule_state, reference_utc=now_utc)
            if ppv:
                ppv["roxie_name"]    = ev["roxie_name"]
                ppv["link"]          = ev["link"]           # primary (stream 2)
                ppv["link_fallback"] = ev.get("link_fallback")  # fallback (stream 1)
                ppv["all_links"]     = ev.get("all_links", [])

        schedule_state.sort(key=lambda x: x.get("starts_at") or now_utc)

        # 4. Extract streams with stream-2-first priority + validation fallback
        print("=" * 60)
        to_extract = [s for s in schedule_state if s.get("link")]
        print(f"Extracting streams for {len(to_extract)} scheduled events (out of {len(schedule_state)} total) ...")

        for i, s in enumerate(to_extract, start=1):
            url = await extract_stream(browser, s, i, len(to_extract))
            if url:
                s["stream_url"] = url
            # If url is None, preserve whatever was previously saved

        # Prepare display names
        for s in schedule_state:
            time_str = fmt_time_pht(s.get("starts_at")).strip()
            base_name = s.get("name", s.get("roxie_name", "Unknown"))
            if time_str and time_str in base_name:
                s["display_name"] = base_name
            else:
                s["display_name"] = f"{base_name} {time_str}".strip()
            s["logo"] = s.get("poster", "")

        await browser.close()

    # 5. Write playlist & schedule state
    print("\n" + "=" * 60)
    save_schedule(schedule_state)
    write_playlist(schedule_state)


if __name__ == "__main__":
    asyncio.run(main())

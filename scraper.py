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


# ── Step 1: scrape roxiestreams NBA via Playwright ───────────────────────────
# FIX #1: Use Playwright instead of requests so JS-rendered tables are visible.

async def get_roxie_events_async(browser) -> list:
    print(f"Scraping {NBA_URL} with Playwright (JS-rendered) ...")
    events  = []
    context = await browser.new_context(user_agent=USER_AGENT)
    page    = await context.new_page()

    try:
        await page.goto(NBA_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        # Give any late JS a moment
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
            # Broad fallback: any row containing a link
            rows = await page.query_selector_all("tr:has(a)")

        print(f"  Rows found: {len(rows)}")

        for row in rows:
            a = await row.query_selector("a")
            if not a:
                continue

            event_name = (await a.inner_text()).strip()
            href       = await a.get_attribute("href")
            if not href:
                continue

            # Extract time from any cell
            cells    = await row.query_selector_all("td")
            time_str = ""
            for cell in cells:
                txt = (await cell.inner_text()).strip()
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            full_link = urljoin(BASE_URL, href)
            events.append({
                "roxie_name"    : event_name,
                "link"          : full_link,
                "roxie_time_str": time_str,
            })
            print(f"  Found: '{event_name}' @ '{time_str}'  -> {full_link}")

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
# FIX #2: Time matching now anchors to today's PHT date, not a raw "now + offsets"
#         to avoid drift when the script runs near midnight or after a delay.

def parse_roxie_time(time_str: str, reference_utc: datetime):
    """
    Convert a bare time string like '7:30 PM' (assumed ET) to a UTC datetime.
    Tries today, tomorrow, and yesterday relative to reference_utc.
    Returns a list of candidate UTC datetimes (best guesses first).
    """
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

    # Anchor to the reference date in ET
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
        extra_http_headers={"Referer": link, "Origin": origin},
    )
    page       = await context.new_page()
    stream_url = None

    # --- PRIMARY: intercept network requests for .m3u8 URLs ---
    intercepted_urls: list[str] = []

    def on_request(req):
        url = req.url
        if ".m3u8" in url and "tracks" not in url:  # grab index.m3u8
            intercepted_urls.append(url)
        elif ".m3u8" in url:
            intercepted_urls.append(url)

    page.on("request", on_request)

    try:
        print(f"  Loading page ...")
        resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

        if not resp or resp.status != 200:
            print(f"  HTTP {resp.status if resp else 'none'} — skipping")
            return None

        print(f"  Page loaded (HTTP {resp.status})")
        await page.wait_for_timeout(2_000)

        # Click stream button
        try:
            btn = page.locator("button.streambutton").first
            if await btn.is_visible(timeout=BUTTON_TIMEOUT):
                print(f"  Clicking stream button ...")
                await btn.click(force=True, timeout=BUTTON_TIMEOUT)
                await page.wait_for_timeout(1_500)
            else:
                print(f"  No stream button visible — clicking page centre")
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(1_500)
        except Exception as e:
            print(f"  Click warning: {e}")

        # Give the player time to fire network requests
        await page.wait_for_timeout(3_000)

        # Check intercepted first
        if intercepted_urls:
            stream_url = intercepted_urls[-1]  # latest is most likely the real one
            print(f"  [intercept] {stream_url[:90]}")
        else:
            # Fallback: wait for clapprPlayer (extended timeout)
            print(f"  No intercepted .m3u8 yet — waiting for clapprPlayer ({CLAPPR_TIMEOUT//1000}s) ...")
            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined' && clapprPlayer.options && clapprPlayer.options.source",
                    timeout=CLAPPR_TIMEOUT,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
                print(f"  [clapprPlayer] {stream_url}")
            except PWTimeoutError:
                print(f"  clapprPlayer timed out — trying JS fallbacks ...")

        # JS fallbacks
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
                        print(f"  [JS fallback] {val[:90]}")
                        break
                except Exception:
                    pass

        # Last resort: check intercepted list again (may have arrived late)
        if not stream_url and intercepted_urls:
            stream_url = intercepted_urls[-1]
            print(f"  [late intercept] {stream_url[:90]}")

    except Exception as e:
        print(f"  Error: {e}")
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
    skipped = 0

    for e in entries:
        url = e.get("stream_url")
        if not url:
            skipped += 1
            print(f"  SKIP (no stream): {e.get('roxie_name', '?')}")
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
    # Snapshot "now" ONCE — used as the stable reference throughout
    now_utc = datetime.now(tz=timezone.utc)
    now_pht = now_utc.astimezone(PHT)
    print(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PHT: {now_pht.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
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

        # 1. Scrape roxie (JS-rendered, uses Playwright now)
        roxie_events = await get_roxie_events_async(browser)

        if not roxie_events:
            print("No NBA events found on roxiestreams.")
            with open(OUTPUT_FILE, "w") as f:
                f.write("#EXTM3U\n")
            await browser.close()
            return

        # 2. Get PPV NBA streams
        print("-" * 60)
        ppv_streams = get_ppv_nba()

        # 3. Match events — pass stable now_utc so time matching is deterministic
        print("-" * 60)
        print("Matching events to PPV.to ...")
        for ev in roxie_events:
            ppv = match_event_to_ppv(ev, ppv_streams, reference_utc=now_utc)
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

        # 4. Extract streams sequentially
        print("=" * 60)
        print(f"Extracting streams for {len(roxie_events)} events ...")

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

"""
NBA M3U Playlist Generator — FIXED
Fixes:
  1. Playwright used for roxie scraping (JS-rendered table)
  2. PPV time-matching anchored to roxie date, not just "now"
  3. CLAPPR timeout extended + network request interception as primary method
  4. Detailed diagnostics so failures are visible
  5. [NEW] Stream links are in <button onclick="showPlayer(...)"> tags.
           If 2 buttons exist, Stream 2's URL is used directly (it's hardcoded
           in the onclick). If only 1 button, falls back to getRandomStream().
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
CLAPPR_TIMEOUT    = 25_000   # 25s
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


def parse_stream_url_from_onclick(onclick: str) -> str | None:
    """
    Extract the stream URL from a button onclick like:
      showPlayer('clappr', 'https://...manifest/video.m3u8')
      showPlayer('clappr', getRandomStream('nba.m3u8', 'daffodil'))
    Returns the direct URL string if found, else None.
    """
    # Match a direct https:// URL inside the onclick
    m = re.search(r"showPlayer\(['\"]clappr['\"],\s*['\"](\bhttps?://[^'\"]+)['\"]", onclick)
    if m:
        return m.group(1)
    return None  # getRandomStream() or unrecognised — let the page handle it


# ── Step 1: scrape roxiestreams NBA via Playwright ───────────────────────────
# Stream links live in <button class="streambutton" onclick="showPlayer(...)">
# Stream 1 uses getRandomStream(), Stream 2 has the URL hardcoded in onclick.
# We always prefer Stream 2 (direct URL). If absent, fall back to Stream 1 page.

async def get_roxie_events_async(browser) -> list:
    print(f"Scraping {NBA_URL} with Playwright (JS-rendered) ...")
    events  = []
    context = await browser.new_context(user_agent=USER_AGENT)
    page    = await context.new_page()

    try:
        await page.goto(NBA_URL, wait_until="networkidle", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(2_000)

        html = await page.content()
        with open("roxie_debug.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("  Saved roxie_debug.html for inspection")

        rows = await page.query_selector_all("table#eventsTable tbody tr")
        if not rows:
            rows = await page.query_selector_all("table tbody tr")
        if not rows:
            rows = await page.query_selector_all("tr:has(button.streambutton)")

        print(f"  Rows found: {len(rows)}")

        for row in rows:
            # Get the event name from the first <a> tag in the row
            a = await row.query_selector("a")
            event_name = (await a.inner_text()).strip() if a else ""
            if not event_name:
                continue

            # Get the event page link (used as referrer / fallback)
            href       = await a.get_attribute("href") if a else None
            event_link = urljoin(BASE_URL, href) if href else ""

            # Extract time string from cells
            cells    = await row.query_selector_all("td")
            time_str = ""
            for cell in cells:
                txt = (await cell.inner_text()).strip()
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            # Find all streambuttons in this row
            buttons = await row.query_selector_all("button.streambutton")
            print(f"  '{event_name}' — {len(buttons)} stream button(s)")

            stream2_url = None
            has_stream2 = len(buttons) >= 2

            if has_stream2:
                onclick2    = await buttons[1].get_attribute("onclick") or ""
                stream2_url = parse_stream_url_from_onclick(onclick2)
                if stream2_url:
                    print(f"    Stream 2 URL (direct): {stream2_url[:80]}")
                else:
                    print(f"    Stream 2 onclick has no direct URL: {onclick2[:80]}")

            events.append({
                "roxie_name"    : event_name,
                "link"          : event_link,   # event page — used as referrer
                "stream2_url"   : stream2_url,  # direct m3u8 if available
                "has_stream2"   : has_stream2,
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

    candidates = parse_roxie_time(time_str, reference_utc)
    window     = timedelta(minutes=TIME_MATCH_WINDOW)
    for s in ppv_streams:
        for candidate in candidates:
            if abs(s["starts_at"] - candidate) <= window:
                print(f"  [time] '{roxie_name}' -> '{s['name']}'")
                return s

    print(f"  [no match] '{roxie_name}' (time_str='{time_str}')")
    return None


# ── Step 4: extract stream URL ────────────────────────────────────────────────
# If stream2_url is already known (from onclick), use it directly.
# Otherwise load the event page and intercept the m3u8 request.

async def extract_stream(browser, event: dict, index: int, total: int):
    name        = event["roxie_name"]
    link        = event["link"]
    stream2_url = event.get("stream2_url")

    print(f"\n[{index}/{total}] Processing: {name}")

    # ── Fast path: Stream 2 URL was hardcoded in onclick ─────────────────────
    if stream2_url:
        final = fix_url(stream2_url)
        print(f"  [stream 2 direct] {final[:90]}")
        return final

    # ── Slow path: load the page and intercept the m3u8 ──────────────────────
    print(f"  No direct stream 2 URL — loading page: {link}")
    origin = get_origin(link)

    context = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Referer": link, "Origin": origin},
    )
    page           = await context.new_page()
    stream_url     = None
    intercepted_urls: list[str] = []

    def on_request(req):
        if ".m3u8" in req.url:
            intercepted_urls.append(req.url)

    page.on("request", on_request)

    try:
        resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if not resp or resp.status != 200:
            print(f"  HTTP {resp.status if resp else 'none'} — skipping")
            return None

        print(f"  Page loaded (HTTP {resp.status})")
        await page.wait_for_timeout(2_000)

        try:
            btn = page.locator("button.streambutton").first
            if await btn.is_visible(timeout=BUTTON_TIMEOUT):
                print(f"  Clicking stream button ...")
                await btn.click(force=True, timeout=BUTTON_TIMEOUT)
                await page.wait_for_timeout(1_500)
            else:
                await page.mouse.click(640, 360)
                await page.wait_for_timeout(1_500)
        except Exception as e:
            print(f"  Click warning: {e}")

        await page.wait_for_timeout(3_000)

        if intercepted_urls:
            stream_url = intercepted_urls[-1]
            print(f"  [intercept] {stream_url[:90]}")
        else:
            print(f"  No intercepted .m3u8 — waiting for clapprPlayer ({CLAPPR_TIMEOUT//1000}s) ...")
            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined' && clapprPlayer.options && clapprPlayer.options.source",
                    timeout=CLAPPR_TIMEOUT,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
                print(f"  [clapprPlayer] {stream_url}")
            except PWTimeoutError:
                print(f"  clapprPlayer timed out — trying JS fallbacks ...")

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

        # 2. Scrape roxie
        print("-" * 60)
        roxie_events = await get_roxie_events_async(browser)

        # 3. Match roxie events -> schedule_state items
        print("-" * 60)
        print("Matching roxie events to schedule items ...")
        for ev in roxie_events:
            ppv = match_event_to_ppv(ev, schedule_state, reference_utc=now_utc)
            if ppv:
                ppv["roxie_name"] = ev["roxie_name"]
                ppv["link"]       = ev["link"]
                ppv["stream2_url"] = ev.get("stream2_url")

        schedule_state.sort(key=lambda x: x.get("starts_at") or now_utc)

        # 4. Extract streams
        print("=" * 60)
        to_extract = [s for s in schedule_state if s.get("link")]
        print(f"Extracting streams for {len(to_extract)} scheduled events (out of {len(schedule_state)} total) ...")

        for i, s in enumerate(to_extract, start=1):
            url = await extract_stream(browser, s, i, len(to_extract))
            if url:
                s["stream_url"] = url

        # Prepare display names
        for s in schedule_state:
            time_str  = fmt_time_pht(s.get("starts_at")).strip()
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

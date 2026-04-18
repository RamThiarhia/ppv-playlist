"""
NBA M3U Playlist Generator — STABLE FIX
Root cause of inconsistency:
  - Roxiestreams serves a partially-rendered/cached page on repeated hits.
    The JS table populates rows asynchronously; reading it too early gives
    fewer rows than actually exist.

Fixes applied:
  1. Cache-bust the roxie URL on every request (timestamp query param)
  2. Wait for row-count to STABILISE (poll until count stops changing)
     instead of a blind wait_for_timeout — this is the key fix.
  3. Retry the entire roxie scrape up to 3 times if row count looks wrong.
  4. Network-intercept .m3u8 as PRIMARY stream extraction method.
  5. Stable reference_utc passed to time-matcher (no drift between runs).
  6. Extended CLAPPR timeout + JS fallbacks.
"""

import asyncio
import re
import time
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
PAGE_TIMEOUT      = 35_000
CLAPPR_TIMEOUT    = 25_000
BUTTON_TIMEOUT    = 5_000
TIME_MATCH_WINDOW = 90

ROXIE_MAX_RETRIES     = 3
ROXIE_STABLE_POLLS    = 5
ROXIE_POLL_INTERVAL   = 1.5
ROXIE_MIN_STABLE_SAME = 3

PHT = timezone(timedelta(hours=8))
ET  = timezone(timedelta(hours=-4))

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

def cache_bust_url(url):
    """Append a timestamp query param to defeat CDN/browser caching."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_cb={int(time.time())}"


# ── Step 1: scrape roxiestreams NBA via Playwright ───────────────────────────

async def _scrape_roxie_once(browser, attempt):
    url     = cache_bust_url(NBA_URL)
    events  = []
    context = await browser.new_context(
        user_agent=USER_AGENT,
        ignore_https_errors=True,
    )
    page = await context.new_page()

    try:
        print(f"  [attempt {attempt}] Loading: {url}")
        await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT)

        # KEY FIX: poll until row count stabilises instead of a blind sleep.
        # The table is JS-populated in batches; reading mid-population returns
        # a partial count. We wait until the count is unchanged for
        # ROXIE_MIN_STABLE_SAME consecutive polls.
        row_selector = "table#eventsTable tbody tr, table tbody tr, tr:has(a)"
        stable_count = 0
        last_count   = -1
        final_count  = 0

        for poll in range(ROXIE_STABLE_POLLS * 4):
            count = await page.locator(row_selector).count()
            if count == last_count and count > 0:
                stable_count += 1
                if stable_count >= ROXIE_MIN_STABLE_SAME:
                    final_count = count
                    print(f"  Row count stable at {count} (poll #{poll+1})")
                    break
            else:
                stable_count = 0
            last_count = count
            await page.wait_for_timeout(int(ROXIE_POLL_INTERVAL * 1000))
        else:
            final_count = await page.locator(row_selector).count()
            print(f"  Row count did not fully stabilise — using {final_count}")

        if attempt == ROXIE_MAX_RETRIES:
            html = await page.content()
            with open("roxie_debug.html", "w", encoding="utf-8") as f:
                f.write(html)
            print("  Saved roxie_debug.html")

        rows     = await page.query_selector_all(row_selector)
        seen     = set()

        for row in rows:
            a = await row.query_selector("a")
            if not a:
                continue
            event_name = (await a.inner_text()).strip()
            href       = await a.get_attribute("href")
            if not href:
                continue
            full_link = urljoin(BASE_URL, href)
            if full_link in seen:
                continue
            seen.add(full_link)

            cells    = await row.query_selector_all("td")
            time_str = ""
            for cell in cells:
                txt = (await cell.inner_text()).strip()
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            events.append({
                "roxie_name"    : event_name,
                "link"          : full_link,
                "roxie_time_str": time_str,
            })
            print(f"  Found: '{event_name}' @ '{time_str}'")

    except Exception as e:
        print(f"  Scrape error (attempt {attempt}): {e}")
    finally:
        await page.close()
        await context.close()

    return events


async def get_roxie_events_async(browser):
    print(f"Scraping {NBA_URL} ...")
    best_result = []

    for attempt in range(1, ROXIE_MAX_RETRIES + 1):
        result = await _scrape_roxie_once(browser, attempt)
        print(f"  Attempt {attempt}: got {len(result)} events")

        if len(result) > len(best_result):
            best_result = result

        if len(result) >= 2:
            # One confirm run to make sure we're not catching a lucky partial
            if attempt < ROXIE_MAX_RETRIES:
                confirm = await _scrape_roxie_once(browser, attempt + 1)
                print(f"  Confirm: got {len(confirm)} events")
                if len(confirm) >= len(result):
                    best_result = confirm
            break

        if attempt < ROXIE_MAX_RETRIES:
            wait_s = 5 * attempt
            print(f"  Only {len(result)} — retrying in {wait_s}s ...")
            await asyncio.sleep(wait_s)

    print(f"  Final: {len(best_result)} NBA events")
    return best_result


# ── Step 2: get PPV.to NBA streams ───────────────────────────────────────────

def get_ppv_nba():
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to from {url} ...")
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code} — next mirror")
                continue
            data = r.json()
            if not data.get("success"):
                print(f"  success=false — next mirror")
                continue

            nba_streams = []
            for group in data.get("streams", []):
                if group.get("category", "").lower() not in ("basketball", "nba"):
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

    print("  WARNING: All PPV mirrors failed")
    return []


# ── Step 3: match roxie → PPV ────────────────────────────────────────────────

def parse_roxie_time(time_str, reference_utc):
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)
    if not m:
        return []
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    ref_et     = reference_utc.astimezone(ET)
    candidates = []
    for day_offset in (0, 1, -1):
        try:
            dt_et = ref_et.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)
            candidates.append(dt_et.astimezone(timezone.utc))
        except Exception:
            pass
    return candidates


def match_event_to_ppv(roxie_ev, ppv_streams, reference_utc):
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

    print(f"  [no match] '{roxie_name}' (time='{time_str}')")
    return None


# ── Step 4: extract stream URL ────────────────────────────────────────────────

async def extract_stream(browser, event, index, total):
    link   = event["link"]
    origin = get_origin(link)
    name   = event["roxie_name"]

    print(f"\n[{index}/{total}] {name}")
    print(f"  URL: {link}")

    context = await browser.new_context(
        user_agent=USER_AGENT,
        extra_http_headers={"Referer": link, "Origin": origin},
    )
    page       = await context.new_page()
    stream_url = None
    intercepted: list[str] = []

    def on_request(req):
        if ".m3u8" in req.url:
            intercepted.append(req.url)

    page.on("request", on_request)

    try:
        resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if not resp or resp.status != 200:
            print(f"  HTTP {resp.status if resp else 'none'} — skipping")
            return None

        print(f"  HTTP {resp.status}")
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

        if intercepted:
            stream_url = intercepted[-1]
            print(f"  [intercept] {stream_url[:90]}")
        else:
            print(f"  Waiting for clapprPlayer ...")
            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined' && clapprPlayer.options && clapprPlayer.options.source",
                    timeout=CLAPPR_TIMEOUT,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
                print(f"  [clapprPlayer] {stream_url}")
            except PWTimeoutError:
                print(f"  clapprPlayer timed out")

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

        if not stream_url and intercepted:
            stream_url = intercepted[-1]
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
        print(f"  [OK] {stream_url[:90]}")
    else:
        print(f"  [FAIL] no stream for '{name}'")

    return stream_url


# ── Step 5: write playlist ────────────────────────────────────────────────────

def write_playlist(entries):
    lines, ok, skipped = ["#EXTM3U"], 0, 0
    for e in entries:
        url = e.get("stream_url")
        if not url:
            skipped += 1
            print(f"  SKIP: {e.get('roxie_name','?')}")
            continue
        link   = e.get("link", "")
        origin = get_origin(link) if link else ""
        name   = e.get("display_name", e.get("roxie_name", "Unknown"))
        logo   = e.get("logo", "")
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
    print(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PHT: {now_utc.astimezone(PHT).strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    print("=" * 60)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        roxie_events = await get_roxie_events_async(browser)
        if not roxie_events:
            print("No NBA events found.")
            with open(OUTPUT_FILE, "w") as f:
                f.write("#EXTM3U\n")
            await browser.close()
            return

        print("-" * 60)
        ppv_streams = get_ppv_nba()

        print("-" * 60)
        print("Matching events ...")
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

        roxie_events.sort(key=lambda x: x.get("starts_at") or now_utc)

        print("=" * 60)
        print(f"Extracting streams for {len(roxie_events)} events ...")
        for i, ev in enumerate(roxie_events, 1):
            ev["stream_url"] = await extract_stream(browser, ev, i, len(roxie_events))

        await browser.close()

    print("\n" + "=" * 60)
    write_playlist(roxie_events)


if __name__ == "__main__":
    asyncio.run(main())

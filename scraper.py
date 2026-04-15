"""
Sports M3U Playlist Generator
- Events + streams : roxiestreams.su (Clappr player, direct JS extraction)
- Names/logos/times: PPV.to API (matched by team names)
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

SPORT_PAGES = {
    "Basketball": urljoin(BASE_URL, "nba"),
    "Racing"    : urljoin(BASE_URL, "motorsports"),
    "Fighting"  : urljoin(BASE_URL, "fighting"),
    "Baseball"  : urljoin(BASE_URL, "mlb"),
    "Hockey"    : urljoin(BASE_URL, "nhl"),
    "Soccer"    : urljoin(BASE_URL, "soccer"),
}

PPV_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE    = "playlist.m3u"
MAX_CONCURRENT = 3
PAGE_TIMEOUT   = 20_000  # ms

# Philippine Time = UTC+8
PHT = timezone(timedelta(hours=8))

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
    """Convert any datetime to Philippine Time and format it."""
    if dt is None:
        return ""
    dt_pht = dt.astimezone(PHT)
    return dt_pht.strftime("%m/%d %I:%M %p PHT")


def normalize(s):
    """Lowercase, strip punctuation — for fuzzy matching."""
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


# ── Step 1: scrape roxiestreams event list ────────────────────────────────────

def get_roxie_events():
    """
    Fetches each sport page, parses the #eventsTable, returns list of:
      { sport, roxie_name, link }
    """
    headers = {"User-Agent": USER_AGENT}
    events  = []

    for sport, url in SPORT_PAGES.items():
        try:
            print(f"  Scraping {sport}: {url}")
            r    = requests.get(url, headers=headers, timeout=15)
            soup = HTMLParser(r.content)

            for row in soup.css("table#eventsTable tbody tr"):
                a = row.css_first("td a")
                if not a:
                    continue
                event_name = a.text(strip=True)
                href       = a.attributes.get("href", "")
                if not href:
                    continue
                events.append({
                    "sport"     : sport,
                    "roxie_name": event_name,
                    "link"      : urljoin(BASE_URL, href),
                })

        except Exception as e:
            print(f"  FAIL {sport}: {e}")

    print(f"  Total roxie events: {len(events)}")
    return events


# ── Step 2: get PPV.to data (names, logos, times) ────────────────────────────

def get_ppv_data():
    """
    Returns list of PPV stream dicts:
      name, poster, starts_at (datetime UTC-aware), category
    """
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to from {url} ...")
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code == 200 and r.json().get("success"):
                streams = []
                for group in r.json().get("streams", []):
                    cat = group.get("category", "Sports")
                    if cat == "24/7 Streams":
                        continue
                    for s in group.get("streams", []):
                        ts = s.get("starts_at", 0)
                        streams.append({
                            "category" : cat,
                            "name"     : s.get("name", ""),
                            "poster"   : s.get("poster", ""),
                            # Store as UTC-aware datetime
                            "starts_at": datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                        })
                print(f"  {len(streams)} PPV streams loaded")
                return streams
        except Exception as e:
            print(f"  FAIL: {e}")
    return []


def match_ppv(roxie_name, ppv_streams):
    """
    Fuzzy-match a roxie event name against PPV.to stream names.
    Returns the best matching PPV stream dict, or None.
    """
    rwords = set(normalize(roxie_name).split())

    best_score  = 0
    best_stream = None

    for s in ppv_streams:
        pwords = set(normalize(s["name"]).split())
        if not rwords or not pwords:
            continue
        overlap = len(rwords & pwords) / max(len(rwords), len(pwords))
        if overlap > best_score:
            best_score  = overlap
            best_stream = s

    return best_stream if best_score >= 0.5 else None


# ── Step 3: Playwright — extract Clappr stream URL ───────────────────────────

async def extract_stream(semaphore, browser, event):
    """
    Opens the roxie event page, clicks the stream button,
    waits for clapprPlayer to initialise, reads its source.
    """
    async with semaphore:
        link   = event["link"]
        origin = get_origin(link)

        context = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={
                "Referer": link,
                "Origin" : origin,
            },
        )
        page       = await context.new_page()
        stream_url = None

        try:
            resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

            if not resp or resp.status != 200:
                print(f"  [{event['roxie_name']}] HTTP {resp.status if resp else 'none'}")
                return None

            # Click the stream button (same as original roxie scraper)
            try:
                btn = page.locator("button.streambutton").first
                await btn.click(force=True, click_count=2, timeout=3_000)
            except Exception:
                try:
                    await page.mouse.click(640, 360)
                except Exception:
                    pass

            # Wait for clapprPlayer (same as original roxie scraper)
            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined'",
                    timeout=8_000,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
            except PWTimeoutError:
                pass

            # Fallback: try other player objects
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
                            break
                    except Exception:
                        pass

        except Exception as e:
            print(f"  [{event['roxie_name']}] error: {e}")
        finally:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

        if stream_url:
            stream_url = fix_url(stream_url)
            print(f"  [OK ] {event['roxie_name']} -> {stream_url[:80]}")
        else:
            print(f"  [---] {event['roxie_name']} -> no stream found")

        return stream_url


# ── Step 4: write playlist ────────────────────────────────────────────────────

def write_playlist(entries):
    lines    = ["#EXTM3U"]
    ok       = 0
    skipped  = 0

    for e in entries:
        url = e.get("stream_url")
        if not url:
            skipped += 1
            continue

        link   = e.get("link", "")
        origin = get_origin(link) if link else ""
        logo   = e.get("logo", "")
        cat    = e.get("category", "Sports")
        name   = e.get("display_name", e.get("roxie_name", "Unknown"))

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{cat}",{name}')
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

    # 1. Scrape roxiestreams for events
    print("Scraping roxiestreams.su ...")
    roxie_events = get_roxie_events()
    if not roxie_events:
        print("No events found on roxiestreams.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    # 2. Get PPV.to data for names/logos/times
    ppv_streams = get_ppv_data()

    # 3. Match each roxie event to PPV.to and build display name in PHT
    for ev in roxie_events:
        ppv = match_ppv(ev["roxie_name"], ppv_streams)
        if ppv:
            # Convert PPV start time to Philippine Time for display
            time_pht       = fmt_time_pht(ppv.get("starts_at"))
            ev["display_name"] = f"{ppv['name']} {time_pht}".strip()
            ev["logo"]         = ppv.get("poster", "")
            ev["category"]     = ppv.get("category", ev["sport"])
            ev["starts_at"]    = ppv.get("starts_at")  # keep UTC for sorting
        else:
            # No PPV match — use roxie name, no time available
            ev["display_name"] = ev["roxie_name"]
            ev["logo"]         = ""
            ev["category"]     = ev["sport"]
            ev["starts_at"]    = now_utc
            print(f"  [no PPV match] {ev['roxie_name']}")

    # Sort by start time (UTC internally, displayed as PHT)
    roxie_events.sort(key=lambda x: x.get("starts_at") or now_utc)

    # 4. Extract stream URLs via Playwright
    print("=" * 60)
    print(f"Extracting streams for {len(roxie_events)} events ...")
    print("-" * 60)

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
        tasks   = [extract_stream(semaphore, browser, ev) for ev in roxie_events]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for ev, url in zip(roxie_events, results):
        ev["stream_url"] = url

    # 5. Write playlist
    write_playlist(roxie_events)


if __name__ == "__main__":
    asyncio.run(main())

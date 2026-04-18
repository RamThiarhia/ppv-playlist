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

BASE_URL  = "https://roxiestreams.su"
NBA_URL   = urljoin(BASE_URL, "nba")

PPV_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE    = "playlist.m3u"
MAX_CONCURRENT = 3
PAGE_TIMEOUT   = 20_000  # ms

# Philippine Time = UTC+8
PHT = timezone(timedelta(hours=8))

# How close two event start times must be to count as a match (minutes)
TIME_MATCH_WINDOW = 60

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
    """Convert UTC datetime to Philippine Time string."""
    if dt is None:
        return ""
    return dt.astimezone(PHT).strftime("%m/%d %I:%M %p PHT")


def normalize(s):
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def slug_to_words(url):
    """
    Extract team name words from a roxie URL slug.
    e.g. /nba/lakers-celtics-2 -> {'lakers', 'celtics'}
    """
    path = urlparse(url).path          # /nba/lakers-celtics-2
    slug = path.rstrip("/").split("/")[-1]  # lakers-celtics-2
    # Remove trailing numbers and split on dashes
    slug = re.sub(r"-\d+$", "", slug)
    return set(slug.lower().split("-")) - {"vs", "at", ""}


# ── Step 1: scrape roxiestreams NBA page ──────────────────────────────────────

def get_roxie_events():
    """
    Scrapes roxiestreams.su/nba event table.
    Also parses the time column if available.
    Returns list of { roxie_name, link, roxie_time_str }
    """
    print(f"Scraping {NBA_URL} ...")
    events = []

    try:
        r    = requests.get(NBA_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
        soup = HTMLParser(r.content)

        for row in soup.css("table#eventsTable tbody tr"):
            cells = row.css("td")
            if not cells:
                continue

            a = row.css_first("td a")
            if not a:
                continue

            event_name = a.text(strip=True)
            href       = a.attributes.get("href", "")
            if not href:
                continue

            # Try to grab time from any cell that looks like a time
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

    except Exception as e:
        print(f"  FAIL: {e}")

    print(f"  Found {len(events)} NBA events on roxie")
    return events


# ── Step 2: get PPV.to NBA streams ───────────────────────────────────────────

def get_ppv_nba():
    """
    Fetches PPV.to and returns only Basketball/NBA streams.
    Each item: { name, poster, starts_at (UTC datetime) }
    """
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
                # PPV uses "Basketball" as category for NBA
                if cat.lower() not in ("basketball", "nba"):
                    continue
                for s in group.get("streams", []):
                    ts = s.get("starts_at", 0)
                    if not ts:
                        continue
                    nba_streams.append({
                        "name"    : s.get("name", ""),
                        "poster"  : s.get("poster", ""),
                        "starts_at": datetime.fromtimestamp(ts, tz=timezone.utc),
                    })

            print(f"  {len(nba_streams)} NBA streams from PPV.to")
            return nba_streams

        except Exception as e:
            print(f"  FAIL {url}: {e}")

    return []


# ── Step 3: match roxie event → PPV stream ───────────────────────────────────

def match_event_to_ppv(roxie_ev, ppv_streams):
    """
    Three strategies, in order:

    1. Name match  — word overlap between roxie name and PPV name (≥50%)
    2. Slug match  — team words from URL slug found in PPV name
    3. Time match  — PPV event starts within TIME_MATCH_WINDOW minutes
                     of any time hints we can extract from roxie

    Returns best PPV stream dict or None.
    """
    roxie_name  = roxie_ev.get("roxie_name", "")
    roxie_link  = roxie_ev.get("link", "")

    # ── Strategy 1: name word overlap ────────────────────────────────────────
    rwords = set(normalize(roxie_name).split())

    best_score  = 0
    best_stream = None

    for s in ppv_streams:
        pwords  = set(normalize(s["name"]).split())
        if not rwords or not pwords:
            continue
        overlap = len(rwords & pwords) / max(len(rwords), len(pwords))
        if overlap > best_score:
            best_score  = overlap
            best_stream = s

    if best_score >= 0.5:
        print(f"    [name match {best_score:.0%}] {roxie_name} -> {best_stream['name']}")
        return best_stream

    # ── Strategy 2: slug word match ───────────────────────────────────────────
    slug_words = slug_to_words(roxie_link)

    if slug_words:
        best_slug_score  = 0
        best_slug_stream = None

        for s in ppv_streams:
            pwords  = set(normalize(s["name"]).split())
            overlap = len(slug_words & pwords) / max(len(slug_words), len(pwords)) if pwords else 0
            if overlap > best_slug_score:
                best_slug_score  = overlap
                best_slug_stream = s

        if best_slug_score >= 0.4:
            print(f"    [slug match {best_slug_score:.0%}] {slug_words} -> {best_slug_stream['name']}")
            return best_slug_stream

    # ── Strategy 3: time window match ────────────────────────────────────────
    # Parse time string from roxie (e.g. "7:30 PM ET")
    # We don't know the date, so we try today and tomorrow in US Eastern (UTC-4/5)
    time_str = roxie_ev.get("roxie_time_str", "")
    time_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)

    if time_match:
        hour   = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm   = time_match.group(3).upper()

        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        # US Eastern = UTC-4 (EDT, used during NBA season)
        ET = timezone(timedelta(hours=-4))
        now_et = datetime.now(tz=ET)

        candidates = []
        for day_offset in (0, 1, -1):
            try:
                dt_et  = now_et.replace(
                    hour=hour, minute=minute, second=0, microsecond=0
                ) + timedelta(days=day_offset)
                dt_utc = dt_et.astimezone(timezone.utc)
                candidates.append(dt_utc)
            except Exception:
                pass

        window = timedelta(minutes=TIME_MATCH_WINDOW)
        for s in ppv_streams:
            ppv_start = s["starts_at"]
            for candidate in candidates:
                if abs(ppv_start - candidate) <= window:
                    print(f"    [time match] {time_str} -> {s['name']} @ {fmt_time_pht(ppv_start)}")
                    return s

    print(f"    [no match] {roxie_name} (slug: {slug_to_words(roxie_link)})")
    return None


# ── Step 4: Playwright — extract Clappr stream URL ───────────────────────────

async def extract_stream(semaphore, browser, event):
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

            # Click stream button (original roxie scraper logic)
            try:
                btn = page.locator("button.streambutton").first
                await btn.click(force=True, click_count=2, timeout=3_000)
            except Exception:
                try:
                    await page.mouse.click(640, 360)
                except Exception:
                    pass

            # Wait for clapprPlayer (original roxie scraper logic)
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

    # 1. Scrape roxie NBA events
    roxie_events = get_roxie_events()
    if not roxie_events:
        print("No NBA events found on roxiestreams.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    # 2. Get PPV.to NBA streams
    ppv_streams = get_ppv_nba()

    # 3. Match each roxie event to a PPV stream
    print("-" * 60)
    print("Matching events to PPV.to ...")
    for ev in roxie_events:
        ppv = match_event_to_ppv(ev, ppv_streams)
        if ppv:
            time_pht           = fmt_time_pht(ppv.get("starts_at"))
            ev["display_name"] = f"{ppv['name']} {time_pht}".strip()
            ev["logo"]         = ppv.get("poster", "")
            ev["starts_at"]    = ppv.get("starts_at")
        else:
            # No match — use roxie name, no logo, no time
            ev["display_name"] = ev["roxie_name"]
            ev["logo"]         = ""
            ev["starts_at"]    = now_utc

    # Sort by start time
    roxie_events.sort(key=lambda x: x.get("starts_at") or now_utc)

    # 4. Extract stream URLs
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

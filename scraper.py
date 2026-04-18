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

OUTPUT_FILE        = "playlist.m3u"
MAX_CONCURRENT     = 3
PAGE_TIMEOUT       = 20_000
TIME_MATCH_WINDOW  = 90   # minutes — wider window to catch more matches

PHT        = timezone(timedelta(hours=8))
ET         = timezone(timedelta(hours=-4))   # US Eastern (EDT)

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
    words = set(slug.lower().split("-")) - {"vs", "at", ""}
    return words


# ── Step 1: scrape roxiestreams NBA ──────────────────────────────────────────

def get_roxie_events():
    print(f"\nScraping {NBA_URL} ...")
    events = []

    try:
        r    = requests.get(NBA_URL, headers={"User-Agent": USER_AGENT}, timeout=15)
        print(f"  HTTP status: {r.status_code}")
        soup = HTMLParser(r.content)

        # ── DEBUG: dump ALL tables found so we can see the real structure ──
        all_tables = soup.css("table")
        print(f"  Tables found on page: {len(all_tables)}")
        for i, tbl in enumerate(all_tables):
            tid = tbl.attributes.get("id", "(no id)")
            print(f"    table[{i}] id={tid}")

        # Try #eventsTable first, then fall back to any table
        rows = soup.css("table#eventsTable tbody tr")
        if not rows:
            print("  #eventsTable not found — trying first table on page")
            rows = soup.css("table tbody tr")

        print(f"  Rows found: {len(rows)}")

        for i, row in enumerate(rows):
            # ── DEBUG: print raw text of every row ──
            cells = row.css("td")
            cell_texts = [c.text(strip=True) for c in cells]
            print(f"    row[{i}]: {cell_texts}")

            a = row.css_first("td a")
            if not a:
                print(f"    row[{i}]: no <a> tag found, skipping")
                continue

            event_name = a.text(strip=True)
            href       = a.attributes.get("href", "")
            if not href:
                print(f"    row[{i}]: no href, skipping")
                continue

            # Grab time string from any cell
            time_str = ""
            for cell in cells:
                txt = cell.text(strip=True)
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            full_link = urljoin(BASE_URL, href)
            print(f"    row[{i}]: name='{event_name}' time='{time_str}' link={full_link}")

            events.append({
                "roxie_name"    : event_name,
                "link"          : full_link,
                "roxie_time_str": time_str,
            })

    except Exception as e:
        print(f"  FAIL: {e}")
        import traceback
        traceback.print_exc()

    print(f"\n  Total roxie NBA events scraped: {len(events)}")
    return events


# ── Step 2: get PPV.to NBA streams ───────────────────────────────────────────

def get_ppv_nba():
    for url in PPV_MIRRORS:
        try:
            print(f"\nFetching PPV.to from {url} ...")
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}")
                continue
            data = r.json()
            if not data.get("success"):
                continue

            all_streams  = []
            nba_streams  = []

            for group in data.get("streams", []):
                cat = group.get("category", "")
                for s in group.get("streams", []):
                    ts = s.get("starts_at", 0)
                    entry = {
                        "category" : cat,
                        "name"     : s.get("name", ""),
                        "poster"   : s.get("poster", ""),
                        "starts_at": datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None,
                    }
                    all_streams.append(entry)
                    if cat.lower() in ("basketball", "nba"):
                        nba_streams.append(entry)

            # ── DEBUG: show all categories and NBA streams ──
            cats = sorted(set(s["category"] for s in all_streams))
            print(f"  All PPV categories: {cats}")
            print(f"  NBA/Basketball streams: {len(nba_streams)}")
            for s in nba_streams:
                print(f"    '{s['name']}' starts_at UTC={s['starts_at']} PHT={fmt_time_pht(s['starts_at'])}")

            return nba_streams

        except Exception as e:
            print(f"  FAIL: {e}")
            import traceback
            traceback.print_exc()

    return []


# ── Step 3: match roxie → PPV ────────────────────────────────────────────────

def match_event_to_ppv(roxie_ev, ppv_streams):
    roxie_name = roxie_ev.get("roxie_name", "")
    roxie_link = roxie_ev.get("link", "")
    time_str   = roxie_ev.get("roxie_time_str", "")

    print(f"\n  Matching: '{roxie_name}' | time='{time_str}' | slug={slug_to_words(roxie_link)}")

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
    print(f"    Strategy 1 (name): best={best_score:.0%} -> '{best_stream['name'] if best_stream else None}'")
    if best_score >= 0.5:
        print(f"    => MATCHED via name")
        return best_stream

    # Strategy 2 — slug word match
    slug_words = slug_to_words(roxie_link)
    if slug_words:
        best_slug_score, best_slug_stream = 0, None
        for s in ppv_streams:
            pwords  = set(normalize(s["name"]).split())
            overlap = len(slug_words & pwords) / max(len(slug_words), len(pwords)) if pwords else 0
            if overlap > best_slug_score:
                best_slug_score, best_slug_stream = overlap, s
        print(f"    Strategy 2 (slug {slug_words}): best={best_slug_score:.0%} -> '{best_slug_stream['name'] if best_slug_stream else None}'")
        if best_slug_score >= 0.4:
            print(f"    => MATCHED via slug")
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
                dt_et  = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)
                dt_utc = dt_et.astimezone(timezone.utc)
                candidates.append(dt_utc)
            except Exception:
                pass

        window = timedelta(minutes=TIME_MATCH_WINDOW)
        print(f"    Strategy 3 (time): parsed hour={hour} min={minute} candidates={[str(c) for c in candidates]}")
        for s in ppv_streams:
            ppv_start = s["starts_at"]
            for candidate in candidates:
                diff = abs(ppv_start - candidate)
                print(f"      vs '{s['name']}' @ {ppv_start} diff={diff}")
                if diff <= window:
                    print(f"    => MATCHED via time")
                    return s
    else:
        print(f"    Strategy 3 (time): no time string to parse")

    print(f"    => NO MATCH")
    return None


# ── Step 4: Playwright extraction ────────────────────────────────────────────

async def extract_stream(semaphore, browser, event):
    async with semaphore:
        link   = event["link"]
        origin = get_origin(link)

        context = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={"Referer": link, "Origin": origin},
        )
        page       = await context.new_page()
        stream_url = None

        try:
            resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            if not resp or resp.status != 200:
                print(f"  [{event['roxie_name']}] HTTP {resp.status if resp else 'none'}")
                return None

            try:
                btn = page.locator("button.streambutton").first
                await btn.click(force=True, click_count=2, timeout=3_000)
            except Exception:
                try:
                    await page.mouse.click(640, 360)
                except Exception:
                    pass

            try:
                await page.wait_for_function(
                    "() => typeof clapprPlayer !== 'undefined'",
                    timeout=8_000,
                )
                stream_url = await page.evaluate("() => clapprPlayer.options.source")
            except PWTimeoutError:
                pass

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

    roxie_events = get_roxie_events()
    if not roxie_events:
        print("No NBA events found on roxiestreams.")
        with open(OUTPUT_FILE, "w") as f:
            f.write("#EXTM3U\n")
        return

    ppv_streams = get_ppv_nba()

    print("\n" + "=" * 60)
    print("Matching events to PPV.to ...")
    for ev in roxie_events:
        ppv = match_event_to_ppv(ev, ppv_streams)
        if ppv:
            time_pht           = fmt_time_pht(ppv.get("starts_at"))
            ev["display_name"] = f"{ppv['name']} {time_pht}".strip()
            ev["logo"]         = ppv.get("poster", "")
            ev["starts_at"]    = ppv.get("starts_at")
        else:
            ev["display_name"] = ev["roxie_name"]
            ev["logo"]         = ""
            ev["starts_at"]    = now_utc

    roxie_events.sort(key=lambda x: x.get("starts_at") or now_utc)

    print("\n" + "=" * 60)
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

    write_playlist(roxie_events)


if __name__ == "__main__":
    asyncio.run(main())

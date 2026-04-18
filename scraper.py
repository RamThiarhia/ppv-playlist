"""
NBA M3U Playlist Generator
- Events + streams : roxiestreams.su/nba
- Names/logos/times: PPV.to API (matched by sport + time window)
- Times displayed  : Philippine Time (UTC+8)
- Headers preserved: Referer, Origin, User-Agent (required by CDN)
"""

import asyncio
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
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

PHT = timezone(timedelta(hours=8))
TIME_MATCH_WINDOW = 60

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ── NBA team abbreviation/nickname → keyword expansion ───────────────────────
ABBREV_MAP = {
    "okc": {"oklahoma", "thunder"}, "gs": {"golden", "warriors"},
    "la": {"angeles", "clippers", "lakers"}, "lac": {"clippers", "angeles"},
    "lal": {"lakers", "angeles"}, "ny": {"new", "york", "knicks"},
    "nyk": {"new", "york", "knicks"}, "no": {"new", "orleans", "pelicans"},
    "nop": {"new", "orleans", "pelicans"}, "sa": {"san", "antonio", "spurs"},
    "sas": {"san", "antonio", "spurs"}, "phx": {"phoenix", "suns"},
    "phi": {"philadelphia", "76ers", "sixers"}, "mil": {"milwaukee", "bucks"},
    "mem": {"memphis", "grizzlies"}, "ind": {"indiana", "pacers"},
    "cha": {"charlotte", "hornets"}, "cle": {"cleveland", "cavaliers"},
    "det": {"detroit", "pistons"}, "tor": {"toronto", "raptors"},
    "orl": {"orlando", "magic"}, "was": {"washington", "wizards"},
    "atl": {"atlanta", "hawks"}, "mia": {"miami", "heat"},
    "bkn": {"brooklyn", "nets"}, "bos": {"boston", "celtics"},
    "chi": {"chicago", "bulls"}, "dal": {"dallas", "mavericks"},
    "den": {"denver", "nuggets"}, "hou": {"houston", "rockets"},
    "min": {"minnesota", "timberwolves"}, "por": {"portland", "trail", "blazers"},
    "sac": {"sacramento", "kings"}, "uta": {"utah", "jazz"},
    "76ers": {"philadelphia", "sixers"}, "sixers": {"philadelphia", "76ers"},
    "blazers": {"portland", "trail"}, "wolves": {"minnesota", "timberwolves"},
    "mavs": {"dallas", "mavericks"}, "cavs": {"cleveland", "cavaliers"},
    "knicks": {"new", "york"}, "nets": {"brooklyn"},
    "spurs": {"san", "antonio"}, "pelicans": {"new", "orleans"},
    "thunder": {"oklahoma", "city"}, "warriors": {"golden", "state"},
    "clippers": {"los", "angeles"}, "lakers": {"los", "angeles"},
    "nuggets": {"denver"}, "timberwolves": {"minnesota"},
}

NOISE_WORDS = {
    "vs", "at", "the", "nba", "game", "basketball",
    "live", "stream", "watch", "online",
}


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

def expand_words(words):
    expanded = set(words)
    for w in words:
        if w in ABBREV_MAP:
            expanded |= ABBREV_MAP[w]
    return expanded - NOISE_WORDS

def name_to_words(name):
    return expand_words(set(normalize(name).split()) - NOISE_WORDS)

def slug_to_words(url):
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return expand_words(set(slug.lower().split("-")) - NOISE_WORDS - {""})

def match_score(rwords, pwords):
    if not rwords or not pwords:
        return 0.0
    return len(rwords & pwords) / min(len(rwords), len(pwords))


# ── Step 1: scrape roxiestreams — group multiple streams per game ─────────────

def get_roxie_events():
    """
    Scrapes roxiestreams.su/nba and GROUPS all stream links that belong to
    the same game under one event entry.

    Roxie lists each stream as a separate table row with the same game name:
      Minnesota Timberwolves vs Denver Nuggets  /nba/timberwolves-nuggets-1
      Minnesota Timberwolves vs Denver Nuggets  /nba/timberwolves-nuggets-2

    We merge these into one event with a list of links:
      { roxie_name, links: [...], roxie_time_str }

    During stream extraction we try ALL links and keep the first working one.
    This is why stream 2 was missing — the old code treated each row as a
    separate event and only wrote one entry per matched PPV game.
    """
    print(f"Scraping {NBA_URL} ...")
    # Use ordered dict keyed by game name to preserve order and group dupes
    grouped = {}

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

            name = a.text(strip=True)
            href = a.attributes.get("href", "")
            if not href:
                continue

            link = urljoin(BASE_URL, href)

            time_str = ""
            for cell in cells:
                txt = cell.text(strip=True)
                if re.search(r"\d{1,2}:\d{2}", txt):
                    time_str = txt
                    break

            if name not in grouped:
                grouped[name] = {
                    "roxie_name"    : name,
                    "links"         : [],
                    "roxie_time_str": time_str,
                }

            grouped[name]["links"].append(link)

    except Exception as e:
        print(f"  FAIL: {e}")

    events = list(grouped.values())
    print(f"  Found {len(events)} unique NBA game(s) on roxie:")
    for ev in events:
        print(f"    '{ev['roxie_name']}' — {len(ev['links'])} stream link(s)")
    return events


# ── Step 2: get PPV.to NBA streams ───────────────────────────────────────────

def get_ppv_nba():
    for url in PPV_MIRRORS:
        try:
            print(f"Fetching PPV.to from {url} ...")
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code} — trying next mirror ...")
                continue
            data = r.json()
            if not data.get("success"):
                print(f"  API returned success=false — trying next mirror ...")
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
                    nba_streams.append({
                        "name"     : s.get("name", ""),
                        "poster"   : s.get("poster", ""),
                        "starts_at": datetime.fromtimestamp(ts, tz=timezone.utc),
                    })

            print(f"  {len(nba_streams)} NBA/Basketball stream(s) from PPV.to")
            return nba_streams

        except Exception as e:
            print(f"  FAIL {url}: {e}")

    print("  All PPV mirrors failed.")
    return None


# ── Step 3: match roxie event → PPV stream ───────────────────────────────────

def match_event_to_ppv(roxie_ev, ppv_streams):
    """
    Four strategies in order:
    1. Name match  — expanded word overlap, min-set denominator
    2. Slug match  — team words from first URL slug
    3. Time match  — game start within TIME_MATCH_WINDOW minutes
    4. Fallback    — only one PPV game available, assign directly
    """
    roxie_name = roxie_ev.get("roxie_name", "")
    first_link = roxie_ev["links"][0] if roxie_ev.get("links") else ""
    rwords     = name_to_words(roxie_name)

    # Strategy 1: name word overlap
    best_score  = 0.0
    best_stream = None
    for s in ppv_streams:
        score = match_score(rwords, name_to_words(s["name"]))
        if score > best_score:
            best_score  = score
            best_stream = s
    if best_score >= 0.5:
        print(f"    [name {best_score:.0%}] '{roxie_name}' → '{best_stream['name']}'")
        return best_stream

    # Strategy 2: slug word match
    slug_words = slug_to_words(first_link)
    if slug_words:
        best_slug_score  = 0.0
        best_slug_stream = None
        for s in ppv_streams:
            score = match_score(slug_words, name_to_words(s["name"]))
            if score > best_slug_score:
                best_slug_score  = score
                best_slug_stream = s
        if best_slug_score >= 0.4:
            print(f"    [slug {best_slug_score:.0%}] {slug_words} → '{best_slug_stream['name']}'")
            return best_slug_stream

    # Strategy 3: time window match
    time_str   = roxie_ev.get("roxie_time_str", "")
    time_match = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", time_str, re.I)
    if time_match:
        hour   = int(time_match.group(1))
        minute = int(time_match.group(2))
        ampm   = time_match.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        ET     = timezone(timedelta(hours=-4))
        now_et = datetime.now(tz=ET)
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
                    print(f"    [time] '{roxie_name}' → '{s['name']}' @ {fmt_time_pht(s['starts_at'])}")
                    return s

    # Strategy 4: single-game fallback
    if len(ppv_streams) == 1:
        print(f"    [fallback] '{roxie_name}' → '{ppv_streams[0]['name']}' (only game available)")
        return ppv_streams[0]

    print(f"    [no match] '{roxie_name}'")
    return None


# ── Step 4: Playwright — try all stream links, keep first working URL ─────────

async def extract_stream_from_link(page, link, roxie_name):
    """Try a single roxie link and return stream URL or None."""
    origin = get_origin(link)
    stream_url = None

    try:
        resp = await page.goto(link, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if not resp or resp.status != 200:
            print(f"    [{roxie_name}] HTTP {resp.status if resp else 'none'} on {link}")
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
        print(f"    [{roxie_name}] error on {link}: {e}")

    return fix_url(stream_url) if stream_url else None


async def extract_stream(semaphore, browser, event):
    """
    Try each stream link for this game in order.
    Returns the first working stream URL found, or None if all fail.
    """
    async with semaphore:
        roxie_name = event["roxie_name"]
        links      = event.get("links", [])
        first_link = links[0] if links else ""
        origin     = get_origin(first_link) if first_link else BASE_URL

        context = await browser.new_context(
            user_agent=USER_AGENT,
            extra_http_headers={
                "Referer": first_link,
                "Origin" : origin,
            },
        )
        page = await context.new_page()

        stream_url = None
        try:
            for i, link in enumerate(links, 1):
                print(f"  [{roxie_name}] trying link {i}/{len(links)}: {link}")
                stream_url = await extract_stream_from_link(page, link, roxie_name)
                if stream_url:
                    print(f"  [OK  {i}/{len(links)}] {roxie_name} → {stream_url[:80]}")
                    break
                else:
                    print(f"  [--- {i}/{len(links)}] {roxie_name} → no stream on this link")

            if not stream_url:
                print(f"  [FAIL] {roxie_name} → all {len(links)} link(s) failed")

        finally:
            try:
                await page.close()
                await context.close()
            except Exception:
                pass

        return stream_url


# ── Step 5: write playlist ────────────────────────────────────────────────────

def write_playlist(entries):
    lines  = ["#EXTM3U"]
    ok     = 0
    no_url = 0

    for e in entries:
        url    = e.get("stream_url") or ""
        links  = e.get("links", [])
        link   = links[0] if links else ""
        origin = get_origin(link) if link else ""
        logo   = e.get("logo", "")
        name   = e.get("display_name", e.get("roxie_name", "Unknown"))

        lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="NBA",{name}')
        lines.append(f'#EXTVLCOPT:http-referrer={link}')
        lines.append(f'#EXTVLCOPT:http-origin={origin}')
        lines.append(f'#EXTVLCOPT:http-user-agent={USER_AGENT}')
        lines.append(url)  # blank if no stream found — entry still appears in playlist

        if url:
            ok += 1
        else:
            no_url += 1
            print(f"  [no url] '{name}' — added with blank stream")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSaved {OUTPUT_FILE}: {ok} with stream, {no_url} without stream")


# ── Step 6: write schedule.json ───────────────────────────────────────────────

def write_schedule(entries, now_utc):
    schedule = []
    for e in entries:
        starts_at = e.get("starts_at")
        if not starts_at:
            continue
        schedule.append({
            "name"         : e.get("display_name", e.get("roxie_name", "")),
            "starts_at_iso": starts_at.isoformat(),
        })

    schedule.sort(key=lambda x: x["starts_at_iso"])

    with open("schedule.json", "w", encoding="utf-8") as f:
        json.dump(schedule, f, indent=2)

    print(f"Saved schedule.json: {len(schedule)} game(s)")
    for g in schedule:
        dt     = datetime.fromisoformat(g["starts_at_iso"])
        status = "PAST" if dt < now_utc else "upcoming"
        print(f"  [{status}] {g['name']} @ {fmt_time_pht(dt)}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now_utc = datetime.now(tz=timezone.utc)
    now_pht = now_utc.astimezone(PHT)
    print(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PHT: {now_pht.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    print("=" * 60)

    roxie_events = get_roxie_events()
    ppv_streams  = get_ppv_nba()

    if ppv_streams is None:
        print("\nCould not reach any PPV mirror — keeping existing playlist unchanged.")
        return

    if len(ppv_streams) == 0:
        print("\nPPV reports NO NBA/Basketball games — writing empty playlist.")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        write_schedule([], now_utc)
        return

    if not roxie_events:
        print("\nNo events found on roxiestreams — writing empty playlist.")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        write_schedule([], now_utc)
        return

    print("-" * 60)
    print("Matching events to PPV.to ...")
    matched_events = []
    for ev in roxie_events:
        ppv = match_event_to_ppv(ev, ppv_streams)
        if ppv:
            time_pht           = fmt_time_pht(ppv.get("starts_at"))
            ev["display_name"] = f"{ppv['name']} {time_pht}".strip()
            ev["logo"]         = ppv.get("poster", "")
            ev["starts_at"]    = ppv.get("starts_at")
            matched_events.append(ev)
        else:
            print(f"    [skipped] '{ev['roxie_name']}' — no match found")

    if not matched_events:
        print("\nNo roxie events matched any PPV game — writing empty playlist.")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        write_schedule([], now_utc)
        return

    matched_events.sort(key=lambda x: x.get("starts_at") or now_utc)
    write_schedule(matched_events, now_utc)

    print("=" * 60)
    print(f"Extracting streams for {len(matched_events)} unique game(s) ...")
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
        tasks   = [extract_stream(semaphore, browser, ev) for ev in matched_events]
        results = await asyncio.gather(*tasks)
        await browser.close()

    for ev, url in zip(matched_events, results):
        ev["stream_url"] = url

    write_playlist(matched_events)


if __name__ == "__main__":
    asyncio.run(main())

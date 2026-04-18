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
import subprocess
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

# Philippine Time = UTC+8
PHT = timezone(timedelta(hours=8))

# How close two event start times must be to count as a match (minutes)
TIME_MATCH_WINDOW = 60

# How many minutes before game start to trigger the pre-game playlist update
TRIGGER_MINUTES_BEFORE = 50

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Path to the scheduler workflow — rewritten after every scrape
SCHEDULER_YML = Path(".github/workflows/scheduler.yml")


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
    path = urlparse(url).path
    slug = path.rstrip("/").split("/")[-1]
    slug = re.sub(r"-\d+$", "", slug)
    return set(slug.lower().split("-")) - {"vs", "at", ""}


# ── Step 1: scrape roxiestreams NBA page ──────────────────────────────────────

def get_roxie_events():
    """
    Scrapes roxiestreams.su/nba event table.
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
    Returns None if the API itself failed (network error / bad response).
    Returns empty list [] if API is fine but no NBA games scheduled.
    """
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
    Three strategies, in order:
    1. Name match  — word overlap between roxie name and PPV name (>=50%)
    2. Slug match  — team words from URL slug found in PPV name
    3. Time match  — PPV event starts within TIME_MATCH_WINDOW minutes
    Returns best PPV stream dict or None.
    """
    roxie_name = roxie_ev.get("roxie_name", "")
    roxie_link = roxie_ev.get("link", "")

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


# ── Step 6: write schedule.json ───────────────────────────────────────────────

def write_schedule(entries, now_utc):
    """
    Save ALL matched game start times to schedule.json.
    Includes games that already started so scheduler dedup still works.
    """
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

    return schedule


# ── Step 7: rewrite scheduler.yml with exact cron times ──────────────────────

def write_scheduler_cron(schedule, now_utc):
    """
    Dynamically rewrites scheduler.yml so the pre-game scheduler only runs
    at the EXACT minutes it's needed — one cron entry per upcoming game,
    scheduled TRIGGER_MINUTES_BEFORE minutes before tip-off.

    Instead of running 288 times a day (*/5), the scheduler now runs
    only N times per day where N = number of games today.

    Example: 2 games at 8:00 AM and 10:00 AM PHT →
      cron fires once at 7:10 AM PHT and once at 9:10 AM PHT.
      That's 2 runs instead of 288. Zero waste.

    If there are no upcoming games, a once-per-day fallback cron is written
    so the workflow stays active and picks up the next day's schedule.json
    after the 12AM fixed update-playlist run.
    """
    SCHEDULER_YML.parent.mkdir(parents=True, exist_ok=True)

    # Load triggered flags so we skip games already handled
    flags_file = Path(".triggered_games.json")
    try:
        triggered = json.loads(flags_file.read_text()) if flags_file.exists() else {}
    except Exception:
        triggered = {}

    cron_entries = []
    for game in schedule:
        try:
            starts_at = datetime.fromisoformat(game["starts_at_iso"])
            game_id   = game["starts_at_iso"].replace(":", "-").replace("+", "_")

            # Skip already-triggered games
            if triggered.get(game_id):
                print(f"  [cron] skipping {game['name']} — already triggered")
                continue

            # Skip games already started
            if starts_at <= now_utc:
                print(f"  [cron] skipping {game['name']} — already started")
                continue

            # Compute the exact UTC trigger time (50 min before start)
            trigger_utc = starts_at - timedelta(minutes=TRIGGER_MINUTES_BEFORE)

            # Skip if the trigger time is already in the past
            if trigger_utc <= now_utc:
                print(f"  [cron] skipping {game['name']} — trigger time already passed")
                continue

            minute = trigger_utc.minute
            hour   = trigger_utc.hour
            day    = trigger_utc.day
            month  = trigger_utc.month

            cron_str = f"{minute} {hour} {day} {month} *"
            label    = (
                f"{game['name']} — trigger at "
                f"{trigger_utc.astimezone(PHT).strftime('%m/%d %I:%M %p PHT')} "
                f"(UTC {trigger_utc.strftime('%H:%M')})"
            )
            cron_entries.append((cron_str, label))
            print(f"  [cron] scheduled: {label}")

        except Exception as e:
            print(f"  [cron] error processing {game.get('name', '?')}: {e}")

    # ── build the cron block ──────────────────────────────────────────────────
    if cron_entries:
        cron_block_lines = []
        for cron_str, label in cron_entries:
            cron_block_lines.append(f"    # {label}")
            cron_block_lines.append(f"    - cron: '{cron_str}'")
        cron_block = "\n".join(cron_block_lines)
        schedule_summary = f"{len(cron_entries)} game trigger(s) scheduled"
    else:
        # No upcoming games — run once a day at 16:05 UTC (12:05 AM PHT) as a
        # keepalive so the workflow stays enabled and catches tomorrow's schedule
        cron_block = (
            "    # No upcoming games — daily keepalive at 12:05 AM PHT (16:05 UTC)\n"
            "    # Will be replaced with exact game times after next update-playlist run\n"
            "    - cron: '5 16 * * *'"
        )
        schedule_summary = "no upcoming games — keepalive only"

    # ── write the full yml ────────────────────────────────────────────────────
    yml_content = f"""\
name: Pre-Game Scheduler
# AUTO-GENERATED by scraper.py — do not edit manually.
# Regenerated after every update-playlist run with exact game trigger times.
# Current schedule: {schedule_summary}

on:
  schedule:
{cron_block}
  workflow_dispatch:

jobs:
  check-and-trigger:
    runs-on: ubuntu-latest
    permissions:
      actions: write
      contents: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Check for upcoming games and trigger playlist update if needed
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          python3 - <<'EOF'
          import json
          import subprocess
          import sys
          from datetime import datetime, timezone, timedelta
          from pathlib import Path

          # ── config ────────────────────────────────────────────────────────
          # Must match TRIGGER_MINUTES_BEFORE in scraper.py
          TRIGGER_MINUTES_BEFORE = 50

          # Acceptance window (minutes). GitHub cron can be a few minutes late,
          # so we accept triggers up to this many minutes past the ideal time.
          # A game whose cron fired late by up to LATE_TOLERANCE min still works.
          LATE_TOLERANCE = 10

          PHT  = timezone(timedelta(hours=8))
          REPO = "${{ github.repository }}"

          # ── load schedule.json ────────────────────────────────────────────
          schedule_file = Path("schedule.json")
          if not schedule_file.exists():
              print("No schedule.json found — skipping.")
              sys.exit(0)

          try:
              games = json.loads(schedule_file.read_text())
          except Exception as e:
              print(f"Could not parse schedule.json: {{e}}")
              sys.exit(0)

          if not games:
              print("schedule.json is empty — no upcoming games.")
              sys.exit(0)

          # ── load triggered flags ──────────────────────────────────────────
          flags_file = Path(".triggered_games.json")
          try:
              triggered = json.loads(flags_file.read_text()) if flags_file.exists() else {{}}
          except Exception:
              triggered = {{}}

          # ── check each game ───────────────────────────────────────────────
          now_utc = datetime.now(tz=timezone.utc)
          now_pht = now_utc.astimezone(PHT)

          print(f"UTC: {{now_utc.strftime('%Y-%m-%d %H:%M:%S')}}")
          print(f"PHT: {{now_pht.strftime('%Y-%m-%d %H:%M:%S')}} (UTC+8)")
          print(f"Checking {{len(games)}} scheduled game(s)...")
          print("-" * 60)

          trigger_game = None

          for game in games:
              try:
                  game_id    = game["starts_at_iso"].replace(":", "-").replace("+", "_")
                  starts_at  = datetime.fromisoformat(game["starts_at_iso"])
                  starts_pht = starts_at.astimezone(PHT)
                  mins_until = (starts_at - now_utc).total_seconds() / 60

                  trigger_at      = starts_at - timedelta(minutes=TRIGGER_MINUTES_BEFORE)
                  mins_to_trigger = (trigger_at - now_utc).total_seconds() / 60

                  print(f"  Game      : {{game['name']}}")
                  print(f"  Start     : {{starts_pht.strftime('%m/%d %I:%M %p PHT')}} ({{mins_until:+.1f}} min)")
                  print(f"  Trigger at: {{trigger_at.astimezone(PHT).strftime('%m/%d %I:%M %p PHT')}} ({{mins_to_trigger:+.1f}} min)")
                  print(f"  ID        : {{game_id}}")

                  if triggered.get(game_id):
                      print(f"  --> already triggered on {{triggered[game_id]}}, skipping")
                      print()
                      continue

                  if mins_until < 0:
                      print(f"  --> game already started, skipping")
                      print()
                      continue

                  # Accept trigger if we are between the ideal trigger time
                  # and LATE_TOLERANCE minutes after it (handles GitHub cron delay)
                  # i.e. mins_until is between (TRIGGER_MINUTES_BEFORE - LATE_TOLERANCE)
                  # and TRIGGER_MINUTES_BEFORE
                  lower = TRIGGER_MINUTES_BEFORE - LATE_TOLERANCE
                  upper = TRIGGER_MINUTES_BEFORE

                  if lower <= mins_until <= upper:
                      trigger_game = dict(game)
                      trigger_game["_id"] = game_id
                      print(f"  --> *** IN WINDOW ({{mins_until:.1f}} min to game) — TRIGGERING! ***")
                      print()
                      break
                  elif mins_until > upper:
                      print(f"  --> not yet in window ({{mins_until:.1f}} min away)")
                  else:
                      print(f"  --> missed window ({{mins_until:.1f}} min, needed {{lower}}–{{upper}})")

                  print()

              except Exception as e:
                  print(f"  Error processing game: {{e}}")
                  print()

          print("-" * 60)

          if trigger_game is None:
              print("\\nNo games in trigger window — nothing to do.")
              sys.exit(0)

          game_id = trigger_game["_id"]
          print(f"\\nTriggering playlist update for: {{trigger_game['name']}}")

          result = subprocess.run(
              ["gh", "workflow", "run", "update-playlist.yml", "--repo", REPO],
              capture_output=True, text=True,
          )
          print(result.stdout)
          if result.returncode != 0:
              print(f"ERROR: {{result.stderr}}")
              sys.exit(1)

          # Mark as triggered — will never fire again for this game
          triggered[game_id] = now_utc.isoformat()
          flags_file.write_text(json.dumps(triggered, indent=2))

          subprocess.run(["git", "config", "user.name",  "github-actions[bot]"], check=True)
          subprocess.run(["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"], check=True)
          subprocess.run(["git", "add", str(flags_file)], check=True)
          subprocess.run(["git", "commit", "-m", f"chore: triggered pre-game update for {{game_id}}"], check=True)
          subprocess.run(["git", "push"], check=True)

          print(f"\\nDone — {{game_id}} is now locked.")
          EOF
"""

    SCHEDULER_YML.write_text(yml_content, encoding="utf-8")
    print(f"\nRewritten {SCHEDULER_YML} — {schedule_summary}")


# ── Step 8: git commit scheduler.yml ─────────────────────────────────────────

def commit_scheduler_yml():
    """Commit the rewritten scheduler.yml so GitHub picks up the new cron times."""
    try:
        # Check if there's actually a change to commit
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet", str(SCHEDULER_YML)],
            capture_output=True,
        )
        subprocess.run(["git", "add", str(SCHEDULER_YML)], check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode == 0:
            print("scheduler.yml unchanged — no commit needed.")
            return
        subprocess.run(
            ["git", "commit", "-m", "chore: update pre-game scheduler cron times"],
            check=True,
        )
        print("Committed updated scheduler.yml")
    except Exception as e:
        print(f"Warning: could not commit scheduler.yml: {e}")


# ── main ──────────────────────────────────────────────────────────────────────

async def main():
    now_utc = datetime.now(tz=timezone.utc)
    now_pht = now_utc.astimezone(PHT)
    print(f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"PHT: {now_pht.strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)")
    print("=" * 60)

    # 1. Scrape roxie NBA events
    roxie_events = get_roxie_events()

    # 2. Get PPV.to NBA streams
    ppv_streams = get_ppv_nba()

    # ── GUARD: PPV API completely unreachable ─────────────────────────────────
    if ppv_streams is None:
        print("\nCould not reach any PPV mirror — keeping existing playlist unchanged.")
        return

    if len(ppv_streams) == 0:
        print("\nPPV reports NO NBA/Basketball games — writing empty playlist.")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        schedule = write_schedule([], now_utc)
        print("\nUpdating scheduler cron (no games today) ...")
        write_scheduler_cron(schedule, now_utc)
        commit_scheduler_yml()
        return

    # ── GUARD: nothing on roxie either ───────────────────────────────────────
    if not roxie_events:
        print("\nNo events found on roxiestreams — writing empty playlist.")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        schedule = write_schedule([], now_utc)
        print("\nUpdating scheduler cron (no events) ...")
        write_scheduler_cron(schedule, now_utc)
        commit_scheduler_yml()
        return

    # 3. Match each roxie event to a PPV stream
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
            print(f"    [skipped] {ev['roxie_name']} — no PPV match, ignoring")

    if not matched_events:
        print("\nNo roxie events matched any PPV game — writing empty playlist.")
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
        schedule = write_schedule([], now_utc)
        print("\nUpdating scheduler cron (no matches) ...")
        write_scheduler_cron(schedule, now_utc)
        commit_scheduler_yml()
        return

    # Sort by start time
    matched_events.sort(key=lambda x: x.get("starts_at") or now_utc)

    # 4. Write schedule.json
    schedule = write_schedule(matched_events, now_utc)

    # 5. Rewrite scheduler.yml with exact cron times for today's games
    print("\nUpdating scheduler cron times ...")
    write_scheduler_cron(schedule, now_utc)

    # 6. Extract stream URLs
    print("=" * 60)
    print(f"Extracting streams for {len(matched_events)} matched event(s) ...")
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

    # 7. Write playlist
    write_playlist(matched_events)


if __name__ == "__main__":
    asyncio.run(main())

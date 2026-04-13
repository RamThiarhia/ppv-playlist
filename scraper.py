"""
PPV.to M3U Playlist Generator
Fetches today's streams from api.ppv.to and generates a playlist.m3u file
"""

import requests
from datetime import datetime, timezone, timedelta


API_MIRRORS = [
    "https://api.ppv.to/api/streams",
    "https://api.ppv.cx/api/streams",
]

OUTPUT_FILE = "playlist.m3u"

# How many hours before/after current time to include streams
HOURS_BEFORE = 2   # include streams that started up to 2 hrs ago
HOURS_AFTER = 24   # include streams starting within next 24 hrs


def get_api_data():
    """Try each mirror until one works."""
    for url in API_MIRRORS:
        try:
            print(f"Trying {url} ...")
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    print(f"Success from {url}")
                    return data
        except Exception as e:
            print(f"Failed {url}: {e}")
    return None


def format_time_label(timestamp):
    """Convert unix timestamp to readable label like 04/13 10:30 AM"""
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.strftime("%m/%d %I:%M %p")


def is_today_stream(starts_at, ends_at, now, start_window, end_window):
    """Check if stream falls within our time window."""
    # Always-live streams (starts_at == 0)
    if starts_at == 0:
        return False  # skip 24/7 channels; set to True if you want them

    event_start = datetime.fromtimestamp(starts_at, tz=timezone.utc)
    event_end = datetime.fromtimestamp(ends_at, tz=timezone.utc) if ends_at else None

    # Include if: event starts within window OR is currently live
    starts_soon = start_window <= event_start <= end_window
    currently_live = event_start <= now and (event_end is None or event_end >= now)

    return starts_soon or currently_live


def build_stream_url(iframe_url):
    """
    Build the embed URL. The iframe points to the player page.
    We use it directly as the stream URL since actual m3u8 requires
    a headless browser to intercept. For direct playlist use, we
    embed the iframe URL as the stream link.
    """
    return iframe_url


def generate_playlist(streams_today):
    """Write streams to M3U format."""
    lines = ["#EXTM3U"]

    for entry in streams_today:
        category = entry["category"]
        name = entry["name"]
        tag = entry.get("tag", "")
        poster = entry.get("poster", "")
        iframe = entry["iframe"]
        starts_at = entry["starts_at"]

        time_label = format_time_label(starts_at) if starts_at else "LIVE"

        # Playlist display name: "Event Name MM/DD HH:MM AM/PM"
        display_name = f"{name} {time_label}"

        lines.append(
            f'#EXTINF:-1 tvg-logo="{poster}" group-title="{category}",{display_name}'
        )
        lines.append(iframe)

    return "\n".join(lines)


def main():
    now = datetime.now(tz=timezone.utc)
    start_window = now - timedelta(hours=HOURS_BEFORE)
    end_window = now + timedelta(hours=HOURS_AFTER)

    print(f"Current UTC time: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Fetching streams from {start_window.strftime('%H:%M')} to {end_window.strftime('%H:%M')} UTC")
    print("-" * 50)

    data = get_api_data()
    if not data:
        print("ERROR: Could not fetch data from any mirror.")
        return

    streams_today = []
    total_skipped = 0

    for category_group in data.get("streams", []):
        category = category_group.get("category", "Unknown")

        # Skip 24/7 always-live channels (optional — remove this to include them)
        if category == "24/7 Streams":
            continue

        for stream in category_group.get("streams", []):
            starts_at = stream.get("starts_at", 0)
            ends_at = stream.get("ends_at", 0)

            if is_today_stream(starts_at, ends_at, now, start_window, end_window):
                streams_today.append({
                    "category": category,
                    "name": stream.get("name", "Unknown"),
                    "tag": stream.get("tag", ""),
                    "poster": stream.get("poster", ""),
                    "iframe": stream.get("iframe", ""),
                    "starts_at": starts_at,
                })

                # Also add substreams if any
                for sub in stream.get("substreams", []):
                    streams_today.append({
                        "category": category,
                        "name": f"{stream.get('name')} ({sub.get('tag', 'Alt')})",
                        "tag": sub.get("tag", ""),
                        "poster": stream.get("poster", ""),
                        "iframe": sub.get("iframe", ""),
                        "starts_at": starts_at,
                    })
            else:
                total_skipped += 1

    # Sort by start time
    streams_today.sort(key=lambda x: x["starts_at"])

    print(f"Found {len(streams_today)} streams for today's window")
    print(f"Skipped {total_skipped} streams outside time window")
    print("-" * 50)

    for s in streams_today:
        time_str = format_time_label(s["starts_at"]) if s["starts_at"] else "LIVE"
        print(f"  [{s['category']}] {s['name']} @ {time_str}")

    playlist_content = generate_playlist(streams_today)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(playlist_content)

    print("-" * 50)
    print(f"Playlist saved to: {OUTPUT_FILE}")
    print(f"Total entries: {len(streams_today)}")


if __name__ == "__main__":
    main()

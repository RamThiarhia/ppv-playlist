# PPV.to M3U Playlist Generator

Auto-updates every 6 hours via GitHub Actions. Fetches today's live sports streams from ppv.to and saves them as a playlist.

## 📺 How to Use

Add this URL to any IPTV player (Kodi, VLC, TiviMate, IPTV Smarters, etc.):

```
https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO_NAME/main/playlist.m3u
```

Replace `YOUR_USERNAME` and `YOUR_REPO_NAME` with your actual GitHub username and repo name.

## 📋 Playlist Format

Each channel is named like:
```
Houston Astros vs. Seattle Mariners 04/13 07:10 AM
Manchester United vs. Leeds United 04/13 02:00 PM
```

Streams are grouped by sport category (Football, Baseball, Ice Hockey, etc.)

## ⚙️ Configuration

Edit `scraper.py` to change these settings at the top:

| Setting | Default | Description |
|---|---|---|
| `HOURS_BEFORE` | `2` | Include streams that started up to N hours ago |
| `HOURS_AFTER` | `24` | Include streams starting within next N hours |

To also include 24/7 channels (South Park, Family Guy, etc.), find this line in `scraper.py` and remove or comment it out:
```python
if category == "24/7 Streams":
    continue
```

## 🔄 Auto-Update Schedule

Playlist refreshes automatically at: **00:00, 06:00, 12:00, 18:00 UTC**

You can also trigger it manually anytime from the **Actions** tab in GitHub.

## 🚀 Setup Guide

See the setup instructions below or check the GitHub Actions workflow in `.github/workflows/update-playlist.yml`.

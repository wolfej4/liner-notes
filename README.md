# Liner Notes

A self-hosted dashboard for your whole Spotify listening history. It imports Spotify's
data export for everything up to today, then records new plays automatically by polling
Spotify every few minutes.

## How the two sources fit together

| Source | Covers | Detail |
|---|---|---|
| **Extended streaming history export** (request from Spotify's Privacy page) | Every play since your account opened, up to the export date | Exact play length, skips, shuffle, device, country, podcasts |
| **Spotify sync** (this container, via the Web API) | Plays from the day you connect onward | Song, artist, album, time. No skips, shuffle, or device. Play length is counted as the full track length |

When a new export overlaps synced plays, the export wins for that stretch of time, so nothing is
double-counted. Re-importing the same files is safe; duplicates are ignored.

## Setup

1. **Create a Spotify app.** Go to <https://developer.spotify.com/dashboard> and create an app.
   Development mode requires the account that owns the app to have Spotify Premium.
   - Which API: **Web API**
   - Redirect URI: your `PUBLIC_URL` + `/auth/callback`, e.g. `https://music.wolfe.house/auth/callback`.
     Spotify requires HTTPS, except for loopback addresses like `http://127.0.0.1:8089/auth/callback`
     (`localhost` isn't accepted).
   - Copy the Client ID and Client secret.
2. **Configure and deploy.**
   - **Portainer:** push this folder to a Git repo, then Stacks > Add stack > **Repository**
     (the web editor can't build images). Add `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and
     `PUBLIC_URL` under **Environment variables** before deploying.
   - **Command line:** `cp .env.example .env`, fill it in, then `docker compose up -d --build`.

   Data lives in `/mnt/user/appdata/liner-notes`.
3. **Put it behind your SSO.** Liner Notes has no login of its own. Anyone who can reach it can
   see your history, import files, and reconnect Spotify. Expose it only through Pangolin with
   Pocket ID authentication, or keep it on the LAN.
4. **Open it,** drop in your export zip, and select **Connect Spotify**.

## Things Spotify limits

- **Only the last 50 plays are available** from the API. At the default 10-minute poll you'd have
  to play 50 songs in 10 minutes to miss any. If the server is down longer than that, the
  dashboard notes the possible gap; your next export fills it in.
- **Sign-in expires after six months.** Spotify now ends refresh tokens six months after consent.
  The dashboard shows the date and offers a Reconnect button two weeks ahead.
- **Private sessions** aren't reported to the API, and podcast plays aren't included in recent plays.
- **Five users per app** in development mode, which is plenty for a personal install.

## Endpoints

| Path | What it does |
|---|---|
| `GET /` | Dashboard |
| `GET /auth/login`, `/auth/callback` | Spotify sign-in |
| `GET /api/status` | Connection and sync status |
| `POST /api/sync` | Sync now |
| `GET /api/records` / `POST /api/records` / `DELETE /api/records` | Read, add, or clear stored plays |
| `POST /api/disconnect` | Forget the Spotify connection |

Mutating `/api/` calls require the header `X-Liner-Notes: 1`.

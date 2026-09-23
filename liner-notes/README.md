# Liner Notes

A self-hosted dashboard for Spotify listening history, for you and a few other people. Each person
imports their Spotify data export for everything up to today, and Liner Notes records their new plays
automatically. Optional weekly and monthly recap emails summarize what they listened to.

## Setup

1. **Deploy.** In Portainer: Stacks > Add stack > **Repository**, pointing at this repo (the web editor
   can't build images). Under **Environment variables**, add `PUBLIC_URL`, the address people will open
   it at, e.g. `https://music.wolfe.house`. Data goes in the `liner-notes-data` Docker volume.
2. **Create the admin account.** Open the site; the first visit goes to a setup page. If you ran the
   earlier single-user version, its history and Spotify connection move into this account.
3. **Create a Spotify app** at <https://developer.spotify.com/dashboard> (the owner needs Spotify Premium).
   Choose **Web API** and register the redirect URI shown on the admin page
   (`https://music.wolfe.house/auth/callback`). Paste the client ID and secret into the admin page.
4. **Add each person to the Spotify app's allowlist.** In development mode Spotify allows five people per
   app, and each one's Spotify email has to be added under **User Management** in the Spotify dashboard
   before they connect. Otherwise Spotify refuses, and Liner Notes tells them to ask you.
5. **Invite people** from the admin page. With email set up, the invite is sent for you; otherwise you
   get a link to send them. Links work for 7 days.
6. **Optional: email.** Add SMTP settings on the admin page to send invites, password links, and recaps.
   "Send me a test recap" checks everything end to end.
7. **Optional: single sign-on.** Create an OIDC client in Pocket ID with the redirect URI shown on the
   admin page (`https://music.wolfe.house/auth/oidc/callback`), then fill in the issuer URL, client ID,
   and secret. Accounts are matched by email; new people still need an invite unless sign-up is open.

Liner Notes now has its own sign-in, so it no longer needs Pangolin's authentication in front of it.
If you keep Pangolin auth on as well, people will sign in twice unless both use Pocket ID.

## Accounts

- **Sign-in:** email and password (scrypt-hashed), single sign-on through any OpenID Connect provider,
  or both. Sign-in is throttled after 5 failed attempts in 15 minutes.
- **Sign-up modes:** invite only (default), open to anyone who can reach the page, or closed.
- **Admins** can invite people, make or remove admins, disable accounts (signs them out and pauses sync),
  create password links, and delete accounts along with their history.
- **Everyone** manages their own Spotify connection, profile, password, and recap emails on the account page.

## Recap emails

Weekly recaps cover Monday through Sunday and go out on the day and hour set on the admin page. Monthly
recaps go out on the 1st. Each recap has total hours with a comparison to the previous period, top
artists and songs, new artists discovered, and the biggest listening day. Nothing is sent for a period
with no plays. Monthly is on by default for new accounts; weekly is opt-in. Every email includes a
one-click unsubscribe link. Anyone can preview their own recap from the account page.

## Settings

Settings saved on the admin page override the container's environment variables. Environment variables
still work as defaults: `PUBLIC_URL`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `POLL_MINUTES`,
`SIGNUP_MODE`, `GOOGLE_FONTS`, `TZ`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USER`,
`SMTP_PASSWORD`, `SMTP_FROM`, `NEWSLETTER_WEEKDAY` (0 = Monday), `NEWSLETTER_HOUR`, `OIDC_ENABLED`,
`OIDC_LABEL`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`.

## Things Spotify limits

- **Only the last 50 plays are available** from the API, so Liner Notes checks every 10 minutes by default.
  If it's down long enough to miss plays, the dashboard notes the gap; the next export fills it in.
- **Sign-in expires after six months.** Spotify ends refresh tokens six months after consent. Each person
  sees the date and gets a Reconnect button two weeks ahead.
- **Synced plays have less detail** than the export: no skips, shuffle, or device, and play length is the
  full track length. Private sessions and podcasts aren't reported.
- **Quota is shared** across all of a developer account's apps. If it runs out, sync pauses until it resets.

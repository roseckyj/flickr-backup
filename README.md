# Flickr album backup

This project uses Flickr's API for album discovery and `wget` for media
transfers, downloading Flickr albums incrementally into `./photos`.

Only photos belonging to albums are downloaded. Photos outside albums are
excluded.

## Setup

1. Copy `.env.example` to `.env`.
2. Fill in the Flickr API and OAuth values.
3. Set `FLICKR_USERNAME`, or provide a full `FLICKR_URL`.
4. Start the daily watcher:

   ```sh
   docker compose up --build -d
   ```

The watcher runs one sync immediately, then follows the standard five-field
`CRON_SCHEDULE` expression. The default is `0 3 * * *` (daily at 03:00).
For example, use `*/30 * * * *` to run every 30 minutes.
`DOWNLOAD_DELAY` pauses between media requests and defaults to `1` second;
increase it if Flickr returns HTTP 429.
`RATE_LIMIT_DELAY` controls the retry cooldown after a 429 and defaults to
`60` seconds.
`DOWNLOAD_USER_AGENT` defaults to a browser-like Chrome User-Agent.
Only one sync can run at a time; overlapping watcher or `run-now` invocations
are skipped using a lock in `./config`.

Albums are stored below `./photos/<album>/`. Existing files are
skipped, and downloaded photo IDs are persisted in `./config/state.json`.

## Run immediately

```sh
docker compose run --rm flickrdownloader run-now
```

## Authentication

The runner uses the complete OAuth token pair from `.env`. Keep `.env` and
`./config` private.

## Logs and verification

```sh
docker compose logs -f flickrdownloader
```

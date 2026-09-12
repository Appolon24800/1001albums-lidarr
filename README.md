# 1001 albums → Lidarr

Each run takes the current album from a [1001albumsgenerator.com](https://1001albumsgenerator.com)
project and makes Lidarr monitor and grab exactly that album. Safe to run
repeatedly: once the album is monitored, further runs that day do nothing.

The script adds each new artist to a dedicated root folder with the tag
`1001 Album Generator`; folder and tag names come from the config. Only the
day's album gets monitored, never a whole discography. An artist already in
your library keeps their root folder and receives only the tag and the new
monitored album.

## Setup

```sh
cp config.example.json config.json
```

Fill in `lidarr_url`, `lidarr_api_key` (Lidarr → Settings → General → API Key)
and `root_folder_path` exactly as Lidarr sees it. `LIDARR_URL` and
`LIDARR_API_KEY` environment variables override the file.

## Run

```sh
python sync_album.py             # sync today's album
python sync_album.py --dry-run   # show what would happen, change nothing
```

With Docker (image published to GHCR from `main`):

```sh
docker run --rm \
  -e LIDARR_URL=http://your-lidarr:8686 \
  -e LIDARR_API_KEY=... \
  ghcr.io/appolon24800/1001albums-lidarr:latest
```

Or mount a `config.json` at `/app/config.json`. The container is one-shot: one
run, one sync, exit.

## Scheduling

One run per day, shortly after your album rolls over. Cron example:

```cron
30 6 * * * docker run --rm -e LIDARR_URL=... -e LIDARR_API_KEY=... ghcr.io/appolon24800/1001albums-lidarr:latest
```

## Development

```sh
pip install -r requirements.txt pytest
python -m pytest tests/
```

Publishing is automatic: pushes to `main` build `latest` + `sha-<short>` tags,
`v*` tags build version tags. The package is private until you make it public
in the GitHub package settings.

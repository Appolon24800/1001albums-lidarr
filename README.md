# 1001 albums → Lidarr

Each run takes the current album from a [1001albumsgenerator.com](https://1001albumsgenerator.com)
project and makes Lidarr monitor and grab exactly that album. Safe to run
repeatedly: once the album is monitored, further runs that day do nothing.

The script adds each new artist to a dedicated root folder with the tag
`1001 Album Generator`; folder and tag names come from the config. Only the
day's album gets monitored, never a whole discography. An artist already in
your library keeps their root folder and receives only the tag and the new
monitored album.

## Configuration

Environment variables, a config file, or both: variables win over the file,
and the file is optional. To use the file, copy the example and edit it:

```sh
cp config.example.json config.json
```

| env var          | config key         | default                |
|------------------|--------------------|------------------------|
| LIDARR_URL       | lidarr_url         | required               |
| LIDARR_API_KEY   | lidarr_api_key     | required               |
| ROOT_FOLDER_PATH | root_folder_path   | required               |
| PROJECT_ID       | project_id         | appolon                |
| TAG              | tag                | 1001 Album Generator   |
| QUALITY_PROFILE  | quality_profile    | first profile in Lidarr |

`ROOT_FOLDER_PATH` / `root_folder_path` must be the path exactly as Lidarr
sees it, e.g. `/tank/media/music/1001 Album Generator`. The API key lives in
Lidarr under Settings → General.

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
  -e ROOT_FOLDER_PATH=/music/1001\ Album\ Generator \
  ghcr.io/appolon24800/1001albums-lidarr:latest
```

Or mount a `config.json` at `/app/config.json` if you prefer the file. The
container is one-shot: one run, one sync, exit.

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

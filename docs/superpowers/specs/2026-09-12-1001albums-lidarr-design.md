# 1001 Albums Generator to Lidarr sync

## Goal

Each execution fetches the current album from a 1001albumsgenerator.com project and makes Lidarr grab exactly that album. Also ships as a Docker image published to GHCR by GitHub Actions.

## Context from research

The project API (`GET https://1001albumsgenerator.com/api/v1/projects/<id>`) returns an object with keys `currentAlbum`, `currentAlbumNotes`, `history`, `name`, `paused`, `shareableUrl`, `updateFrequency`. `currentAlbum` has `artist`, `name`, `releaseDate`, `spotifyId`, `appleMusicId`, and more, but no MusicBrainz ID, so the script resolves the album through Lidarr's own MusicBrainz-backed search.

Lidarr facts that shape the design:

- Lidarr has no playlist or library object. Grouping is done with tags and root folders.
- An artist has exactly one root folder. Moving an artist to a "1001" folder would relocate their entire discography, so existing artists are never moved, only tagged.
- Album-level monitoring on artist add is unreliable: the `albums` array with a monitored flag can be silently dropped. The robust pattern (used by digarr) is add the artist unmonitored at the strategy level, then `PUT /api/v1/album/monitor` with the album's DB id, then trigger an `AlbumSearch` command.

## Decisions

- Grouping: new artists go into a dedicated root folder (path from config) and every touched artist gets a tag, default label "1001 Album Generator". Artists that already exist elsewhere in the library keep their root folder and only receive the tag plus the monitored album.
- Credentials: `config.json` next to the script, overridable via `LIDARR_URL` and `LIDARR_API_KEY` env vars.
- Execution model: one-shot. One run syncs the current album and exits. Scheduling is external (cron, systemd timer). The Docker container is equally one-shot; no internal scheduler.
- Image: amd64 only, `python:3.12-slim` base, `requests` pinned in `requirements.txt`, entrypoint `python sync_album.py`.
- Publishing: GitHub Actions on push to `main` (tags `latest` + git SHA) and on `v*` tags (semver tags), logged in to `ghcr.io` with the automatic `GITHUB_TOKEN`, `packages: write` permission declared in the workflow.
- Idempotency keyed on the album's MusicBrainz ID, not the artist. The same artist can appear multiple times in the 1001 list with different albums.

## Repository layout

`~/Projects/1001albums-lidarr/`, its own git repository:

- `sync_album.py`, the script
- `requirements.txt`
- `config.example.json`
- `README.md`
- `Dockerfile`
- `.github/workflows/docker.yml`
- `.gitignore` (ignores `config.json`)
- `tests/` with a few pytest cases for the pure logic

## Config schema

`config.json`:

```json
{
  "lidarr_url": "https://lidarr.example.com",
  "lidarr_api_key": "...",
  "project_id": "appolon",
  "root_folder_path": "/music/1001 Album Generator",
  "tag": "1001 Album Generator",
  "quality_profile": "any name, optional, defaults to Lidarr's first profile"
}
```

Env overrides: `LIDARR_URL`, `LIDARR_API_KEY`. In the container, env vars or a config mounted at `/app/config.json`.

## Flow per execution

1. `GET` the project API. If `paused` is true, log it and exit 0.
2. Resolve via `GET /api/v1/search?term="<artist> <album>"`, take the first album-type result, giving `foreignAlbumId` and the artist's `foreignArtistId`. No match: print "could not resolve <artist> - <album>" on stderr, exit 1. Never guess a partial match.
3. Idempotency check, `GET /api/v1/album?foreignAlbumId=<mbid>`. Present and already monitored: print "already added", exit 0 with no writes at all (an album that predates this script does not get tagged retroactively; no-op runs are side-effect free). Present and unmonitored: continue with the normal flow, which naturally reduces to monitor + search since the artist necessarily exists.
4. Ensure tag exists (`GET/POST /api/v1/tag`), ensure root folder exists (`GET/POST /api/v1/rootfolder`), look up quality and metadata profile ids (`GET /api/v1/qualityprofile`, `GET /api/v1/metadataprofile`, first/default unless `quality_profile` names one).
5. Artist lookup `GET /api/v1/artist?foreignArtistId=`:
   - New: `POST /api/v1/artist` with the MBID, name, the 1001 root folder, the tag, `monitor: "none"` so no discography grab, and the target album flagged monitored in the `albums` payload.
   - Existing: `PATCH /api/v1/artist` to add the tag if missing. Root folder untouched.
6. Re-fetch the album to get its Lidarr DB id, `PUT /api/v1/album/monitor` `{albumIds: [id], monitored: true}`. This is deliberate even for new artists, working around the silent-drop quirk.
7. `POST /api/v1/command` `{"name": "AlbumSearch", "albumIds": [id]}` so Lidarr searches indexers immediately. Always queued on a run that gets this far; when the album already had all its files, Lidarr finds nothing missing and the command is a no-op.
8. Print a summary line: artist, album, action taken (added artist / existing artist), command queued.

`--dry-run` runs steps 1 to 3, prints the resolved MBIDs and what it would do, exits without writing anything.

## Error handling

Every HTTP call fails loudly on non-2xx: message on stderr naming the endpoint, exit 1, no partial writes where avoidable (the add-then-monitor sequence is idempotent on re-run, so a failure mid-sequence is safe to retry next execution). Timeouts set explicitly (10 s per call). No retries, no backoff. Errors never raise raw tracebacks to the user; top-level handler prints a one-liner.

## Testing

- pytest with canned responses for the pure functions: parse of the generator payload, resolution-pick logic, already-added decision, add-artist payload shape, existing-artist tag merge.
- One live `--dry-run` against the real Lidarr and the real project API to confirm wiring before first real run.

## Scheduling

Not built in. README shows a cron example running the container once daily shortly after the album rollover, e.g. `docker run --rm ghcr.io/<user>/1001albums-lidarr:latest`.

## Publishing

Workflow `docker.yml` triggers on push to `main` and on `v*` tags, uses `docker/setup-buildx-action`, `docker/login-action` against `ghcr.io` with `${{ secrets.GITHUB_TOKEN }}`, `docker/metadata-action` for tags, `docker/build-push-action` with `push: true`, amd64 platform, GHA build cache. The local repo gets a GitHub remote at first-publish time, confirmed with the user before anything is pushed. The resulting package starts private and can be made public from the GitHub package settings if wanted.

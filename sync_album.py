#!/usr/bin/env python3
"""Sync the current 1001 Albums Generator album into Lidarr.

Each run fetches the album of the day from a 1001albumsgenerator.com project,
resolves it against MusicBrainz through Lidarr's search endpoint, then makes
sure exactly that album is monitored and searched in Lidarr. Runs are
idempotent: if the album is already monitored the script exits without
writing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

GENERATOR_URL = "https://1001albumsgenerator.com/api/v1/projects/{project_id}"
TIMEOUT = 10
REFRESH_WAIT_SECONDS = 15


class SyncError(Exception):
    """A failure reported to the user as a one-line message."""


# --- pure helpers, covered by tests ---


def normalize(text: str) -> str:
    """Casefold and drop punctuation for tolerant title comparison."""
    cleaned = "".join(c for c in text.casefold() if c.isalnum() or c.isspace())
    return " ".join(cleaned.split())


def pick_album_result(results: list, artist: str, album: str):
    """Return the first album-type search result matching artist and title.

    Both the artist name and the album title must match after normalization.
    Anything less gets reported as unresolvable instead of guessed.
    """
    for r in results:
        if r.get("type") != "album":
            continue
        r_artist = (r.get("artist") or {}).get("artistName", "")
        if normalize(r_artist) == normalize(artist) and normalize(r.get("title", "")) == normalize(album):
            return r
    return None


def build_add_artist_payload(hit: dict, tag_id: int, quality_profile_id: int,
                             metadata_profile_id: int, root_folder: str) -> dict:
    """Payload for POST /api/v1/artist adding an artist around one album."""
    return {
        "foreignArtistId": hit["artist"]["foreignArtistId"],
        "artistName": hit["artist"]["artistName"],
        "monitored": True,
        "monitorNewItems": "none",
        "qualityProfileId": quality_profile_id,
        "metadataProfileId": metadata_profile_id,
        "rootFolderPath": root_folder,
        "tags": [tag_id],
        "albums": [{"foreignAlbumId": hit["foreignAlbumId"], "monitored": True}],
        "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
    }


def merged_tags(current: list, tag_id: int):
    """Return the new tags list for the artist, or None if unchanged."""
    if tag_id in current:
        return None
    return sorted(set(current) | {tag_id})


def pick_profile(profiles: list, name: str | None):
    """Profile id by name, or the first profile when no name given."""
    if name:
        for p in profiles:
            if p.get("name") == name:
                return p["id"]
        available = ", ".join(p.get("name", "?") for p in profiles)
        raise SyncError(f"profile '{name}' not found; available: {available}")
    return profiles[0]["id"]


def load_config(path: Path) -> dict:
    """Merge config.json with LIDARR_URL / LIDARR_API_KEY env overrides."""
    data = {}
    if path.exists():
        data = json.loads(path.read_text())
    cfg = {
        "lidarr_url": os.environ.get("LIDARR_URL") or data.get("lidarr_url"),
        "lidarr_api_key": os.environ.get("LIDARR_API_KEY") or data.get("lidarr_api_key"),
        "project_id": data.get("project_id", "appolon"),
        "root_folder_path": data.get("root_folder_path"),
        "tag": data.get("tag", "1001 Album Generator"),
        "quality_profile": data.get("quality_profile"),
    }
    if not cfg["lidarr_url"] or not cfg["lidarr_api_key"]:
        raise SyncError(
            "lidarr_url and lidarr_api_key are required: put them in "
            f"{path} or export LIDARR_URL / LIDARR_API_KEY"
        )
    if not cfg["root_folder_path"]:
        raise SyncError(f"root_folder_path is required in {path}")
    return cfg


# --- API clients ---


class Generator:
    def __init__(self, session: requests.Session):
        self.session = session

    def current_album(self, project_id: str):
        try:
            resp = self.session.get(GENERATOR_URL.format(project_id=project_id), timeout=TIMEOUT)
            resp.raise_for_status()
            payload = resp.json()
        except requests.RequestException as e:
            raise SyncError(f"could not fetch 1001albumsgenerator project: {e}") from e
        album = payload.get("currentAlbum")
        if not album or not album.get("artist") or not album.get("name"):
            raise SyncError("project payload has no current album")
        return album, bool(payload.get("paused"))


class Lidarr:
    def __init__(self, session: requests.Session, base_url: str, api_key: str):
        self.session = session
        self.base = base_url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}

    def get(self, path: str, **params):
        return self._request("GET", path, params=params or None)

    def post(self, path: str, body: dict):
        return self._request("POST", path, body=body)

    def put(self, path: str, body: dict):
        return self._request("PUT", path, body=body)

    def _request(self, method: str, path: str, params=None, body=None):
        try:
            resp = self.session.request(
                method, self.base + path, params=params, json=body,
                headers=self.headers, timeout=TIMEOUT,
            )
        except requests.RequestException as e:
            raise SyncError(f"{method} {path} failed: {e}") from e
        if not resp.ok:
            raise SyncError(f"{method} {path} returned {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    # Convenience wrappers -------------------------------------------------

    def album_by_mbid(self, mbid: str) -> list:
        return self.get("/api/v1/album", foreignAlbumId=mbid) or []

    def artist_by_mbid(self, mbid: str) -> list:
        return self.get("/api/v1/artist", foreignArtistId=mbid) or []

    def ensure_tag(self, label: str) -> int:
        for t in self.get("/api/v1/tag") or []:
            if t.get("label") == label:
                return t["id"]
        return self.post("/api/v1/tag", {"label": label})["id"]

    def ensure_root_folder(self, path: str) -> None:
        want = path.rstrip("/")
        for f in self.get("/api/v1/rootfolder") or []:
            if f.get("path", "").rstrip("/") == want:
                return
        self.post("/api/v1/rootfolder", {"path": path})

    def album_db_id(self, artist_resource: dict, album_mbid: str):
        """Lidarr DB id of the album, refreshing the artist if it is not there yet."""
        if artist_resource:
            for album in artist_resource.get("albums") or []:
                if album.get("foreignAlbumId") == album_mbid:
                    return album["id"]
            # Album not in the DB yet, typically released after the artist was
            # last refreshed. Refresh, then poll until it shows up.
            self.post("/api/v1/command", {"name": "ArtistRefresh",
                                          "artistId": artist_resource["id"]})
            deadline = time.time() + REFRESH_WAIT_SECONDS
            while time.time() < deadline:
                time.sleep(3)
                found = self.album_by_mbid(album_mbid)
                if found:
                    return found[0]["id"]
        else:
            found = self.album_by_mbid(album_mbid)
            if found:
                return found[0]["id"]
        raise SyncError(
            f"album {album_mbid} not found in Lidarr after refresh; "
            "check the MusicBrainz metadata for this release"
        )


# --- main flow ---


def sync(lidarr: Lidarr, cfg: dict, session: requests.Session, dry_run: bool = False) -> None:
    album, paused = Generator(session).current_album(cfg["project_id"])
    artist_name, album_name = album["artist"], album["name"]

    if paused:
        print(f"project is paused, nothing to do ({artist_name} - {album_name} is still current)")
        return

    results = lidarr.get("/api/v1/search", term=f"{artist_name} {album_name}") or []
    hit = pick_album_result(results, artist_name, album_name)
    if hit is None:
        raise SyncError(
            f"could not resolve '{artist_name} - {album_name}' in Lidarr search; "
            "no MusicBrainz match, nothing was added"
        )

    album_mbid = hit["foreignAlbumId"]
    artist_mbid = hit["artist"]["foreignArtistId"]
    existing_artist = lidarr.artist_by_mbid(artist_mbid)
    existing_album = lidarr.album_by_mbid(album_mbid)

    if any(a.get("monitored") for a in existing_album):
        print(f"already added: {artist_name} - {album_name}")
        return

    if dry_run:
        print(f"resolved: {artist_name} - {album_name}")
        print(f"  album MBID:    {album_mbid}")
        print(f"  artist MBID:   {artist_mbid}")
        if existing_artist:
            print(f"  artist already in Lidarr, id {existing_artist[0]['id']}")
            print(f"  would add tag '{cfg['tag']}' if missing, monitor this album, "
                  "queue AlbumSearch")
        else:
            print("  artist not in Lidarr yet")
            print(f"  would create tag '{cfg['tag']}' and root folder "
                  f"'{cfg['root_folder_path']}', add the artist there with only "
                  "this album monitored, queue AlbumSearch")
        return

    tag_id = lidarr.ensure_tag(cfg["tag"])

    if not existing_artist:
        lidarr.ensure_root_folder(cfg["root_folder_path"])
        quality_id = pick_profile(lidarr.get("/api/v1/qualityprofile") or [], cfg["quality_profile"])
        metadata_id = pick_profile(lidarr.get("/api/v1/metadataprofile") or [], None)
        created = lidarr.post("/api/v1/artist", build_add_artist_payload(
            hit, tag_id, quality_id, metadata_id, cfg["root_folder_path"]))
        album_id = lidarr.album_db_id(created, album_mbid)
        action = (f"added artist '{artist_name}' to '{cfg['root_folder_path']}' "
                  f"with tag '{cfg['tag']}'")
    else:
        artist = existing_artist[0]
        new_tags = merged_tags(artist.get("tags") or [], tag_id)
        if new_tags is not None:
            artist["tags"] = new_tags
            lidarr.put(f"/api/v1/artist/{artist['id']}", artist)
        album_id = lidarr.album_db_id(artist, album_mbid)
        action = f"used existing artist '{artist_name}', root folder untouched"

    # Lidarr applies the albums array in the add payload unreliably; this
    # explicit call makes the monitor state certain.
    lidarr.put("/api/v1/album/monitor", {"albumIds": [album_id], "monitored": True})
    lidarr.post("/api/v1/command", {"name": "AlbumSearch", "albumIds": [album_id]})
    print(f"done: monitoring and searching '{artist_name} - {album_name}' ({action})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Add today's 1001 Albums Generator album to Lidarr.")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would happen without changing anything")
    parser.add_argument("--config", type=Path, default=None,
                        help="path to config.json (default: next to the script)")
    args = parser.parse_args(argv)

    config_path = args.config or Path(__file__).with_name("config.json")
    try:
        cfg = load_config(config_path)
        with requests.Session() as session:
            sync(Lidarr(session, cfg["lidarr_url"], cfg["lidarr_api_key"]), cfg, session,
                 dry_run=args.dry_run)
    except SyncError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

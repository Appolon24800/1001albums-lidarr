import json

import pytest
import requests

from sync_album import (
    Generator, Lidarr, SyncError, build_add_artist_payload, load_config,
    merged_tags, normalize, pick_album_result, pick_profile, sync,
)


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.content = json.dumps(payload).encode() if payload is not None else b""
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, payload, status=200):
        self.response = FakeResponse(payload, status)
        self.calls = []

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        return self.response

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


HIT = {
    "type": "album",
    "title": "Meat Puppets II",
    "foreignAlbumId": "album-mbid",
    "artist": {"artistName": "Meat Puppets", "foreignArtistId": "artist-mbid"},
}


# --- pure helpers ---------------------------------------------------------


def test_normalize_strips_punctuation_and_case():
    assert normalize("Meat Puppets II!") == normalize("meat puppets ii")
    assert normalize("  OK   Computer  ") == "ok computer"


def test_pick_album_result_matches_on_normalized_title_and_artist():
    other = {"type": "album", "title": "Too High to Die", "foreignAlbumId": "x",
             "artist": {"artistName": "Meat Puppets", "foreignArtistId": "artist-mbid"}}
    results = [{"type": "artist", "artistName": "Meat Puppets"}, other, HIT]
    assert pick_album_result(results, "Meat Puppets", "Meat Puppets II") is HIT


def test_pick_album_result_refuses_partial_matches():
    results = [dict(HIT, title="Meat Puppets III")]
    assert pick_album_result(results, "Meat Puppets", "Meat Puppets II") is None
    assert pick_album_result([], "a", "b") is None


def test_build_add_artist_payload_shape():
    payload = build_add_artist_payload(HIT, tag_id=7, quality_profile_id=1,
                                       metadata_profile_id=2, root_folder="/music/1001")
    assert payload["foreignArtistId"] == "artist-mbid"
    assert payload["rootFolderPath"] == "/music/1001"
    assert payload["tags"] == [7]
    assert payload["monitorNewItems"] == "none"
    assert payload["albums"] == [{"foreignAlbumId": "album-mbid", "monitored": True}]
    assert payload["addOptions"] == {"monitor": "none", "searchForMissingAlbums": False}


def test_merged_tags_returns_none_when_unchanged():
    assert merged_tags([3, 7], 7) is None
    assert merged_tags([3], 7) == [3, 7]
    assert merged_tags([], 1) == [1]


def test_pick_profile_by_name_default_and_error():
    profiles = [{"id": 1, "name": "Lossless"}, {"id": 2, "name": "MP3"}]
    assert pick_profile(profiles, "MP3") == 2
    assert pick_profile(profiles, None) == 1
    with pytest.raises(SyncError, match="Lossless, MP3"):
        pick_profile(profiles, "FLAC")


# --- config ---------------------------------------------------------------


def test_load_config_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "lidarr_url": "http://file", "lidarr_api_key": "file-key",
        "root_folder_path": "/music/1001", "project_id": "file-project",
    }))
    monkeypatch.setenv("LIDARR_URL", "http://env")
    monkeypatch.setenv("LIDARR_API_KEY", "env-key")
    monkeypatch.setenv("PROJECT_ID", "env-project")
    cfg = load_config(path)
    assert cfg["lidarr_url"] == "http://env"
    assert cfg["lidarr_api_key"] == "env-key"
    assert cfg["project_id"] == "env-project"
    assert cfg["tag"] == "1001 Album Generator"


def test_load_config_env_only_without_any_file(tmp_path, monkeypatch):
    for var in ("LIDARR_URL", "LIDARR_API_KEY", "PROJECT_ID",
                "ROOT_FOLDER_PATH", "TAG", "QUALITY_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LIDARR_URL", "http://env")
    monkeypatch.setenv("LIDARR_API_KEY", "env-key")
    monkeypatch.setenv("ROOT_FOLDER_PATH", "/music/1001")
    cfg = load_config(tmp_path / "does-not-exist.json")
    assert cfg["lidarr_url"] == "http://env"
    assert cfg["root_folder_path"] == "/music/1001"
    assert cfg["project_id"] == "appolon"
    assert cfg["tag"] == "1001 Album Generator"


def test_load_config_empty_env_var_falls_back_to_file(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "lidarr_url": "http://file", "lidarr_api_key": "file-key",
        "root_folder_path": "/music/1001",
    }))
    monkeypatch.setenv("LIDARR_URL", "")
    cfg = load_config(path)
    assert cfg["lidarr_url"] == "http://file"


def test_load_config_requires_credentials_and_root_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("LIDARR_URL", raising=False)
    monkeypatch.delenv("LIDARR_API_KEY", raising=False)
    with pytest.raises(SyncError, match="lidarr_url and lidarr_api_key"):
        load_config(tmp_path / "missing.json")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"lidarr_url": "http://x", "lidarr_api_key": "k"}))
    with pytest.raises(SyncError, match="root_folder_path"):
        load_config(path)


# --- generator ------------------------------------------------------------


def test_generator_returns_album_and_paused_flag():
    payload = {"currentAlbum": {"artist": "Meat Puppets", "name": "Meat Puppets II"},
               "paused": False}
    album, paused = Generator(FakeSession(payload)).current_album("appolon")
    assert album["name"] == "Meat Puppets II"
    assert paused is False


def test_generator_raises_on_missing_album_or_http_error():
    with pytest.raises(SyncError, match="no current album"):
        Generator(FakeSession({})).current_album("appolon")
    with pytest.raises(SyncError, match="1001albumsgenerator"):
        Generator(FakeSession({}, status=500)).current_album("appolon")


# --- lidarr client --------------------------------------------------------


def test_lidarr_request_reports_http_errors():
    lidarr = Lidarr(FakeSession({"message": "boom"}, status=400), "http://x", "key")
    with pytest.raises(SyncError, match="returned 400"):
        lidarr.get("/api/v1/artist")


# --- sync orchestration ---------------------------------------------------


class RecordingLidarr(Lidarr):
    """Lidarr client with stubbed reads, canned POST results, recorded writes."""

    def __init__(self, artist_response=None, album_response=None):
        super().__init__(None, "http://x", "key")
        self.artist_response = artist_response or []
        self.album_response = album_response or []
        self.writes = []

    def get(self, path, **params):
        return {
            "/api/v1/search": [HIT],
            "/api/v1/artist": self.artist_response,
            "/api/v1/album": self.album_response,
            "/api/v1/qualityprofile": [{"id": 1, "name": "Lossless"}],
            "/api/v1/metadataprofile": [{"id": 2, "name": "Standard"}],
            "/api/v1/rootfolder": [],
            "/api/v1/tag": [],
        }[path]

    def post(self, path, body):
        self.writes.append(("POST", path, body))
        if path == "/api/v1/artist":
            return {"id": 99, "albums": [{"id": 5, "foreignAlbumId": "album-mbid"}]}
        return {"id": 1}

    def put(self, path, body):
        self.writes.append(("PUT", path, body))
        return {}

    def ensure_tag(self, label):
        return 7


CFG = {"project_id": "appolon", "tag": "1001 Album Generator",
       "root_folder_path": "/music/1001", "quality_profile": None}


def generator_session(paused=False):
    return FakeSession({"currentAlbum": {"artist": "Meat Puppets", "name": "Meat Puppets II"},
                        "paused": paused})


def test_sync_already_monitored_album_is_a_noop(capsys):
    lidarr = RecordingLidarr(album_response=[{"id": 5, "monitored": True}])
    sync(lidarr, CFG, generator_session())
    assert lidarr.writes == []
    assert "already added" in capsys.readouterr().out


def test_sync_paused_project_is_a_noop(capsys):
    lidarr = RecordingLidarr()
    sync(lidarr, CFG, generator_session(paused=True))
    assert lidarr.writes == []
    assert "paused" in capsys.readouterr().out


def test_sync_dry_run_resolves_and_writes_nothing(capsys):
    lidarr = RecordingLidarr()
    sync(lidarr, CFG, generator_session(), dry_run=True)
    assert lidarr.writes == []
    out = capsys.readouterr().out
    assert "album MBID:    album-mbid" in out
    assert "would" in out


def test_sync_new_artist_adds_monitors_and_searchs():
    lidarr = RecordingLidarr()
    sync(lidarr, CFG, generator_session())
    add = next(w for w in lidarr.writes if w[1] == "/api/v1/artist")
    assert add[2]["foreignArtistId"] == "artist-mbid"
    assert add[2]["albums"] == [{"foreignAlbumId": "album-mbid", "monitored": True}]
    assert ("POST", "/api/v1/rootfolder", {"path": "/music/1001"}) in lidarr.writes
    assert ("PUT", "/api/v1/album/monitor", {"albumIds": [5], "monitored": True}) in lidarr.writes
    assert ("POST", "/api/v1/command", {"name": "AlbumSearch", "albumIds": [5]}) in lidarr.writes


def test_sync_existing_artist_tags_monitors_and_searchs():
    artist = {"id": 3, "tags": [4], "albums": [{"id": 5, "foreignAlbumId": "album-mbid"}]}
    lidarr = RecordingLidarr(artist_response=[artist],
                             album_response=[{"id": 5, "monitored": False}])
    sync(lidarr, CFG, generator_session())
    put_artist = next(w for w in lidarr.writes if w[1] == "/api/v1/artist/3")
    assert put_artist[2]["tags"] == [4, 7]
    # Existing artist: no new root folder, no add, just tag + monitor + search.
    assert not any(w[1] == "/api/v1/artist" and w[0] == "POST" for w in lidarr.writes)
    assert not any(w[1] == "/api/v1/rootfolder" for w in lidarr.writes)
    assert ("PUT", "/api/v1/album/monitor", {"albumIds": [5], "monitored": True}) in lidarr.writes
    assert ("POST", "/api/v1/command", {"name": "AlbumSearch", "albumIds": [5]}) in lidarr.writes


def test_sync_existing_artist_without_the_tag_change_skips_put():
    artist = {"id": 3, "tags": [4, 7], "albums": [{"id": 5, "foreignAlbumId": "album-mbid"}]}
    lidarr = RecordingLidarr(artist_response=[artist],
                             album_response=[{"id": 5, "monitored": False}])
    sync(lidarr, CFG, generator_session())
    assert not any(w[1] == "/api/v1/artist/3" for w in lidarr.writes)


def test_sync_unresolvable_album_raises():
    lidarr = RecordingLidarr()
    lidarr.get = lambda path, **params: (
        [dict(HIT, title="Wrong Title")] if path == "/api/v1/search" else [])
    with pytest.raises(SyncError, match="could not resolve"):
        sync(lidarr, CFG, generator_session())

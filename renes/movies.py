"""Find TAS movies for a project's ROM on TASVideos (public API) and download them.

A movie only replays in sync on the ROM revision it was made for, and TASVideos records each
game version's SHA-1 (of the whole .nes file, as our knowledge base stores it). So: fetch the NES
publication list (cached for a week in out/), keep publications whose game title matches the ROM's
name, then check each candidate game's versions against our ROM's SHA-1.

Downloads go to roms/tas/<id>M/ (git-ignored with the ROMs; one folder per movie, named by its
TASVideos id so `renes tas` names the run after it).
"""
import json
import re
import time
import urllib.request
from pathlib import Path

API = "https://tasvideos.org/api/v1"
CACHE_DAYS = 7
PAGES = 30


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "renes (local NES reverse-engineering tool)"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def norm(name: str) -> str:
    """'Example Game (U) (PRG 0)' / 'Example Game.' -> 'examplegame'."""
    name = re.sub(r"\.nes$", "", name, flags=re.I)
    name = re.sub(r"[\(\[].*?[\)\]]", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def title_game(title: str) -> str:
    """'NES Example Game "1 player" by Someone in 12:34.567' -> 'Example Game'."""
    t = title[4:] if title.startswith("NES ") else title
    return re.split(r' "| by ', t, maxsplit=1)[0]


def publications(cache: Path) -> list[dict]:
    if cache.exists() and time.time() - cache.stat().st_mtime < CACHE_DAYS * 86400:
        return json.loads(cache.read_text(encoding="utf-8"))
    pubs = []
    for page in range(1, PAGES + 1):
        batch = json.loads(_get(f"{API}/Publications?Systems=NES&PageSize=100&CurrentPage={page}"))
        if not batch:
            break
        pubs += batch
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(pubs), encoding="utf-8")
    return pubs


def find(rom_name: str, sha1: str, cache: Path) -> list[dict]:
    """Publications of this game, exact-ROM ones first, each with the ROM revision it was made for."""
    want = norm(rom_name)
    found = [p for p in publications(cache) if norm(title_game(p["title"])) == want]
    versions: dict[int, list[dict]] = {}
    out = []
    for p in found:
        if p["gameId"] not in versions:
            versions[p["gameId"]] = json.loads(_get(f"{API}/Games/{p['gameId']}")).get("versions", [])
        made_for = next((v for v in versions[p["gameId"]] if v["id"] == p["gameVersionId"]), None)
        out.append({"id": p["id"], "title": p["title"], "frames": p["frames"], "emulator": p["emulatorVersion"],
                    "file": p["movieFileName"],
                    "rom": "exact ROM" if made_for and made_for["sha1"].upper() == sha1.upper()
                           else f"other revision ({made_for['name']})" if made_for else "unknown ROM",
                    "download": f"https://tasvideos.org/{p['id']}M?handler=Download"})
    return sorted(out, key=lambda m: (m["rom"] != "exact ROM", m["frames"]))


def download(pub_id: int, tas_dir: Path) -> Path:
    folder = tas_dir / f"{pub_id}M"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{pub_id}M.zip"
    path.write_bytes(_get(f"https://tasvideos.org/{pub_id}M?handler=Download"))
    return path

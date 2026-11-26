"""TAS movies: fm2/bk2 parsing, resync fixes, rare-change RAM, and a short replay in Mesen."""
import hashlib
import base64
import io
import zipfile

import pytest

from conftest import EXAMPLE_ROM, needs_mesen
from renes import tas
from renes.kb import KB, NON_ROM, Label
from renes.tas import MovieError, apply_fixes, load_movie, parse_bk2, parse_fm2, rare_changes

FM2 = """version 3
romFilename Test
romChecksum base64:AAECAwQFBgcICQoLDA0ODw==
palFlag 0
|1|........|........||
|0|R......A|........||
|0|....T...|........||
|0|.L..T.B.|........||
"""


def test_fm2_parses_buttons_header_and_reset():
    movie = parse_fm2(FM2)
    assert movie.frames == ["none", "right+a", "start", "left+start+b"]
    assert movie.resets == {0} and movie.header["romFilename"] == "Test"
    assert base64.b64encode(movie.rom_md5).decode() == "AAECAwQFBgcICQoLDA0ODw=="


@pytest.mark.parametrize("text", [FM2.replace("palFlag 0", "palFlag 1"), "version 3\nbinary 1\n|0|........||\n",
                                  "version 3\n"], ids=["pal", "binary", "empty"])
def test_unsupported_fm2_is_rejected(text):
    with pytest.raises(MovieError):
        parse_fm2(text)


def bk2(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Header.txt", "GameName Test\n")
        z.writestr("Input Log.txt", "[Input]\nLogKey:#Reset|Power|#P1 Up|P1 Down|P1 Left|P1 Right|P1 Start|"
                                    "P1 Select|P1 B|P1 A|\n" + "\n".join(lines) + "\n[/Input]\n")
    return buf.getvalue()


def test_bk2_maps_buttons_by_log_key():
    movie = parse_bk2(bk2(["|r.|U.......|", "|..|...R..BA|", "|..|........|"]))
    assert movie.frames == ["up", "right+b+a", "none"] and movie.resets == {0}


def test_load_movie_reads_zipped_fm2_and_bk2(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("run.fm2", FM2)
    (tmp_path / "a.zip").write_bytes(buf.getvalue())
    (tmp_path / "b.bk2").write_bytes(bk2(["|..|U.......|"]))
    assert len(load_movie(tmp_path / "a.zip").frames) == 4
    assert load_movie(tmp_path / "b.bk2").frames == ["up"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("run.bk2", bk2(["|..|U.......|"]))
    (tmp_path / "c.zip").write_bytes(buf.getvalue())
    assert load_movie(tmp_path / "c.zip").frames == ["up"]   # the TASVideos download, as saved


def test_fixes_refer_to_original_frames_and_move_resets():
    movie = tas.Movie({}, ["a", "b", "c", "d", "e"], {3})
    fixed = apply_fixes(movie, [{"frame": 1, "insert": 2}, {"frame": 4, "delete": 1}])
    assert fixed.frames == ["a", "none", "none", "b", "c", "d"] and fixed.resets == {5}
    with pytest.raises(MovieError):
        apply_fixes(movie, [{"frame": 99, "insert": 1}])


def test_rare_changes_lists_slow_bytes_with_timelines(tmp_path):
    samples = []
    for i in range(100):
        ram = bytearray(16)
        ram[1] = i % 256                  # changes every sample: too often
        ram[2] = i // 40                  # 0,1,2: a level-like byte
        samples.append((i * 30, bytes(ram)))
    with KB(tmp_path / "k.db") as kb:
        kb.add_label(Label(NON_ROM, 2, "Level", "ram", "candidate", "claude"))
        text = rare_changes(samples, kb)
    assert "$0002   2 changes  Level [candidate]" in text and "1200:01 2400:02" in text
    assert "$0001" not in text


@needs_mesen
def test_short_replay_archives_states_and_screenshots(tmp_path):
    from renes import rom as romlib

    kb_path = tmp_path / "example.db"
    with KB(kb_path) as kb:
        kb.store_rom(romlib.load(EXAMPLE_ROM))
        md5 = hashlib.md5(kb.rom().raw[16:]).digest()
    frames = ["none"] * 40 + ["start"] * 4 + ["none"] * 200 + ["right"] * 120
    movie = tas.Movie({"romChecksum": "base64:" + base64.b64encode(md5).decode()}, frames, {0})
    r, samples = tas.replay(kb_path, movie, "t", every=120, shots_every=180, shots_dir=tmp_path / "shots",
                            log=lambda *_: None)
    assert r.frames == len(frames) and r.states == 4 and len(r.shots) == 3 and r.run_code > 3000
    assert samples[-1][0] == len(frames)
    with KB(kb_path) as kb:
        kind, = kb.db.execute("SELECT kind FROM runs WHERE id=?", (r.run_id,)).fetchone()
    assert kind == "tas:t"

    wrong = tas.Movie({"romChecksum": "base64:" + base64.b64encode(b"x" * 16).decode()}, frames, set())
    with pytest.raises(MovieError):
        tas.replay(kb_path, wrong, "w", log=lambda *_: None)


def test_second_controller_is_kept():
    from renes import tas

    text = "version 3\nromChecksum base64:AAAA\n|0|R......A|.......A||\n|0|........|........||\n"
    m = tas.parse_fm2(text)
    assert m.frames == ["right+a/a", "none"]

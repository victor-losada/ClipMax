from clipmax.kick import pick_variant
from clipmax.mentions import HypeDetector, MentionMatcher, normalize
from clipmax.transcriber import Segment, clean_segments, parse_whisper_json


def test_normalize_emotes_and_accents():
    assert normalize("¡Qué HUMILLACIÓN [emote:37226:KEKW] @Westcol!") == "que humillacion kekw westcol!"


def test_matcher_finds_aliases_with_word_boundaries(cfg):
    m = MentionMatcher(cfg["streamers"])
    assert m.find("gear of nos se picó") == {"gearofnos": 1}
    assert m.find("el WEST y Gear hablando") == {"westcol": 1, "gearofnos": 1}
    assert m.find("una película western") == {}          # 'west' dentro de otra palabra
    assert m.find_others("west me robó", "westcol") == {}
    assert m.find_others("west me robó", "gearofnos") == {"westcol": 1}


def test_matcher_fuzzy_for_whisper_typos(cfg):
    m = MentionMatcher(cfg["streamers"])
    assert m.find("el wescool dijo", fuzzy=False) == {}
    assert m.find("el wescool dijo", fuzzy=True).get("westcol") == 1


def test_hype_detector():
    h = HypeDetector(["kekw", "clip", "😂"])
    assert h.is_hype("JAJAJAJAJA")
    assert h.is_hype("xdd")
    assert h.is_hype("clip eso")
    assert h.is_hype("😂😂")
    assert h.is_hype("NOOOOO QUE HIZO")
    assert not h.is_hype("buenas tardes chat")


def test_parse_whisper_json_and_clean():
    raw = """{"transcription": [
      {"timestamps": {"from": "00:00:00,000", "to": "00:00:02,500"}, "offsets": {"from": 0, "to": 2500}, "text": " Hola Gear"},
      {"offsets": {"from": 2500, "to": 4000}, "text": " Subtítulos realizados por la comunidad de Amara.org"},
      {"offsets": {"from": 4000, "to": 5000}, "text": " [Música]"},
      {"offsets": {"from": 5000, "to": 6000}, "text": " otra vez"},
      {"offsets": {"from": 6000, "to": 7000}, "text": " otra vez"},
      {"offsets": {"from": 7000, "to": 8000}, "text": " otra vez"}
    ]}""".encode()
    segs = clean_segments(parse_whisper_json(raw))
    assert [s.text for s in segs] == ["Hola Gear", "otra vez", "otra vez"]
    assert segs[0].end == 2.5


def test_clean_segments_keeps_normal_text():
    segs = [Segment(0, 1, "Westcol, venga"), Segment(1, 2, "gracias por ver el video")]
    assert [s.text for s in clean_segments(segs)] == ["Westcol, venga"]


def test_pick_variant():
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,FRAME-RATE=60
1080p60/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3500000,RESOLUTION=1280x720,FRAME-RATE=60
720p60/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=852x480
480p30/playlist.m3u8
"""
    base = "https://cdn.example/api/channel.m3u8"
    assert pick_variant(master, base, 720) == "https://cdn.example/api/720p60/playlist.m3u8"
    assert pick_variant(master, base, 1080).endswith("1080p60/playlist.m3u8")
    assert pick_variant(master, base, 240).endswith("480p30/playlist.m3u8")
    assert pick_variant("#EXTM3U\n#EXTINF:2,\nseg1.ts", base, 720) == base

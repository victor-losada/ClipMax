import random

from clipmax.detector import (build_moments, detect_chat_mention_spikes, detect_chat_peaks, detect_sync,
                              mark_shared, moving_avg, rank, run_detection, rescore_with_transcripts)
from clipmax.mentions import MentionMatcher


def _buckets(n=600, spikes=(300,), base=10, t0=1_000_000.0):
    rnd = random.Random(1)
    rows = []
    for i in range(n):
        c = max(0, int(rnd.gauss(base, 2)))
        hype = rnd.randint(0, 2)
        if any(0 <= i - s < 4 for s in spikes):
            c += 60
            hype += 30
        if c:
            rows.append({"bucket_ts": t0 + i * 5, "n_msgs": c, "n_hype": hype, "n_users": c})
    return rows


def test_moving_avg_centered():
    assert moving_avg([0, 0, 9, 0, 0], 3) == [0, 3, 3, 3, 0]


def test_detect_chat_peaks_finds_single_spike(cfg):
    peaks = detect_chat_peaks(_buckets(), cfg["deteccion"])
    assert len(peaks) == 1
    assert abs(peaks[0]["ts"] - (1_000_000 + 300 * 5)) <= 15
    assert peaks[0]["z"] > 5 and peaks[0]["hype_frac"] > 0.3
    assert 0 < peaks[0]["score"] <= 1


def test_no_peaks_on_flat_chat(cfg):
    assert detect_chat_peaks(_buckets(spikes=()), cfg["deteccion"]) == []


def test_small_chat_is_measured_against_itself(cfg):
    # Un chat pequeño (2 msg/bucket) con un pico de +12 también cuenta: la vara es su propio ritmo.
    rows = _buckets(base=2, spikes=(200,))
    for r in rows:
        if 1_000_000 + 200 * 5 <= r["bucket_ts"] < 1_000_000 + 204 * 5:
            r["n_msgs"] -= 48
    assert len(detect_chat_peaks(rows, cfg["deteccion"])) == 1


def test_mention_spikes(cfg):
    rows = [{"bucket_ts": 1000.0 + i * 5, "target": "gearofnos", "n": 5} for i in range(4)]
    rows += [{"bucket_ts": 5000.0, "target": "gearofnos", "n": 1}]
    spikes = detect_chat_mention_spikes(rows, cfg["deteccion"])
    assert len(spikes) == 1 and spikes[0]["target"] == "gearofnos"


def test_sync_between_streams():
    peaks = {"westcol": [{"ts": 100.0, "score": 0.8}], "gearofnos": [{"ts": 130.0, "score": 0.6}],
             "otro": [{"ts": 900.0, "score": 0.9}]}
    sync = detect_sync(peaks, 45)
    assert {(s["slug"], s["con"]) for s in sync} == {("westcol", "gearofnos"), ("gearofnos", "westcol")}


def test_build_moments_merges_and_prioritizes_pair(cfg):
    p = cfg["deteccion"]
    sigs = [
        {"slug": "westcol", "ts": 1000.0, "tipo": "pico_chat", "score": 0.8, "detalle": {"z": 7}},
        {"slug": "westcol", "ts": 1010.0, "tipo": "mencion_voz", "score": 0.8, "detalle": {"target": "gearofnos", "texto": "gear"}},
        {"slug": "otro", "ts": 1000.0, "tipo": "pico_chat", "score": 0.8, "detalle": {"z": 7}},
        {"slug": "westcol", "ts": 5000.0, "tipo": "pico_chat", "score": 0.5, "detalle": {"z": 4}},
    ]
    moments = build_moments(sigs, p, {"westcol": 1.0, "otro": 1.0}, ["westcol", "gearofnos"])
    assert len(moments) == 3
    merged = [m for m in moments if m["slug"] == "westcol" and m["start_ts"] < 2000][0]
    assert merged["componentes"]["pareja"] is True
    assert merged["componentes"]["senales"] == {"pico_chat": 1, "mencion_voz": 1}
    other = [m for m in moments if m["slug"] == "otro"][0]
    assert merged["score"] > other["score"] * 2
    mark_shared(moments)
    assert merged["componentes"]["mismo_suceso"] == ["otro"]
    ranked = rank(moments, 2)
    assert [m["rank"] for m in ranked] == [1, 2] and ranked[0] is merged


def test_run_detection_and_rescore(cfg, db):
    s = db.get_or_create_session("2026-09-23")
    sid = s["id"]
    rows = _buckets(spikes=(300,))
    db.add_chat_buckets([(sid, "westcol", r["bucket_ts"], r["n_msgs"], r["n_users"], r["n_hype"]) for r in rows])
    db.add_chat_buckets([(sid, "otro", r["bucket_ts"], r["n_msgs"], r["n_users"], r["n_hype"]) for r in _buckets(spikes=(100,))])
    moments = run_detection(cfg, db, s)
    assert len(moments) == 2
    west = [m for m in moments if m["slug"] == "westcol"][0]
    # Transcripción con mención a Gear y un tema de X: sube la puntuación.
    db.add_segments(sid, "westcol", [(west["start_ts"] + 10, west["start_ts"] + 15, "Gear usted me robó los diamantes"),
                                     (west["start_ts"] + 16, west["start_ts"] + 30, "venga y me lo dice en la cara parce " * 4)],
                    "candidato")
    before = west["score"]
    matcher = MentionMatcher(cfg["streamers"])
    out = rescore_with_transcripts(cfg, db, s, matcher, ["diamantes"])
    west2 = [m for m in out if m["slug"] == "westcol"][0]
    assert west2["score"] > before
    assert west2["componentes"]["pareja"] is True and west2["rank"] == 1
    assert west2["componentes"]["temas_x"] == ["diamantes"]
    # Idempotente: re-puntuar no acumula bonus.
    again = [m for m in rescore_with_transcripts(cfg, db, s, matcher, ["diamantes"]) if m["slug"] == "westcol"][0]
    assert again["score"] == west2["score"]

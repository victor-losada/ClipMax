"""Grabador (con un 'stream' local), lector de chat y llamada a Claude con cliente simulado."""

import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from clipmax import brain
from clipmax.chat import ChatListener
from clipmax.kick import ChannelInfo
from clipmax.mentions import MentionMatcher
from clipmax.recorder import StreamRecorder

from .conftest import has_ffmpeg


class FakeKick:
    """Simula la API de Kick: en vivo la primera vez, luego offline."""

    def __init__(self, url):
        self.url = url
        self.calls = 0

    def get_channel(self, slug, use_cache=True):
        self.calls += 1
        return ChannelInfo(slug, 1, 555, self.calls == 1, self.url, "titulo", 100, 9)

    def resolve_variant(self, url, max_h):
        return url


@pytest.mark.skipif(not has_ffmpeg(), reason="ffmpeg no instalado")
def test_recorder_records_a_part_and_reconnects(cfg, db, tmp_path):
    src = tmp_path / "fuente.ts"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=8",
                    "-f", "lavfi", "-i", "sine=duration=8", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-f", "mpegts", str(src)], check=True)
    cfg["grabacion"]["resolver"] = "api"
    cfg["grabacion"]["reintento_s"] = 0.2
    s = db.get_or_create_session("2026-09-23")
    rec = StreamRecorder(cfg, db, s, cfg["streamers"][0], FakeKick(str(src)))
    rec.start()
    deadline = time.time() + 30
    while time.time() < deadline and not (db.list_parts(s["id"], "westcol") and rec.status["estado"] == "offline"):
        time.sleep(0.2)
    rec.stop()
    rec.join(10)
    parts = db.list_parts(s["id"], "westcol")
    assert len(parts) == 1 and parts[0]["ended_at"] and parts[0]["bytes"] > 10_000
    assert parts[0]["path"].endswith("westcol_parte001.ts")
    assert rec.status["estado"] == "detenido"


def test_chat_listener_aggregates_and_flushes(cfg, db):
    s = db.get_or_create_session("2026-09-23")
    listener = ChatListener(cfg, db, s, cfg["streamers"][0], FakeKick(""), MentionMatcher(cfg["streamers"]))
    msgs = [{"id": "1", "content": "JAJAJA gear se picó", "sender": {"username": "a"}},
            {"id": "1", "content": "JAJAJA gear se picó", "sender": {"username": "a"}},  # duplicado por 2 canales
            {"id": "2", "content": "[emote:1:KEKW]", "sender": {"username": "b"}},
            {"id": "3", "content": "hola chat", "sender": {"username": "c"}}]
    for m in msgs:
        listener._on_message(None, json.dumps({"event": "App\\Events\\ChatMessageEvent", "data": json.dumps(m)}))
    listener._flush(force=True)
    buckets = db.chat_buckets(s["id"], "westcol")
    assert sum(b["n_msgs"] for b in buckets) == 3
    assert sum(b["n_hype"] for b in buckets) == 2
    assert [m["target"] for m in db.chat_mentions(s["id"], "westcol")] == ["gearofnos"]
    assert listener.status["total"] == 3
    assert len(db.query("SELECT * FROM chat_messages")) == 3


def test_decide_api_builds_expected_request(cfg, db, monkeypatch):
    raw = {"titulo_video": "T", "resumen_del_dia": "", "lore_para_manana": "", "guion": [], "mejores_momentos": [],
           "descartados": [], "notas_editor": ""}
    seen = {}

    class Stream:
        def __init__(self, params):
            seen.update(params)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                    cache_read_input_tokens=0, server_tool_use=None)
            return SimpleNamespace(model="claude-opus-5", stop_reason="end_turn", usage=usage,
                                   content=[SimpleNamespace(type="thinking", thinking=""),
                                            SimpleNamespace(type="text", text=json.dumps(raw))],
                                   to_json=lambda: "{}")

    class FakeClient:
        class messages:
            @staticmethod
            def count_tokens(**_):
                return SimpleNamespace(input_tokens=1000)

        class beta:
            class messages:
                @staticmethod
                def stream(**params):
                    return Stream(params)

    monkeypatch.setattr(brain, "_client", lambda: FakeClient())
    session = {"id": 1, "fecha": "2026-09-23"}
    out = brain.decide_api(cfg, db, session, "material del día")
    assert out["titulo_video"] == "T"
    assert seen["model"] == "claude-opus-5"
    assert seen["thinking"] == {"type": "adaptive"}
    assert seen["output_config"]["effort"] == "high"
    assert "format" not in seen["output_config"]           # el esquema de la decisión no cabe en la API
    assert seen["fallbacks"] == "default" and seen["betas"] == [brain.FALLBACK_BETA]
    assert "Prompt maestro" in seen["system"]
    run = db.claude_runs()[0]
    assert run["costo_usd"] == pytest.approx(1000 * 5 / 1e6 + 500 * 25 / 1e6)


def test_decide_api_reads_json_without_schema(cfg, db, monkeypatch):
    import anthropic
    import httpx2

    raw = {"titulo_video": "T", "guion": []}
    calls = []

    class Stream:
        def __init__(self, params):
            calls.append(params)
            self.params = params

        def __enter__(self):
            if "format" in self.params.get("output_config", {}):
                resp = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
                raise anthropic.BadRequestError(
                    "The compiled grammar is too large, which would cause performance issues.", response=resp,
                    body=None)
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                    cache_read_input_tokens=0, server_tool_use=None)
            return SimpleNamespace(model="claude-opus-5", stop_reason="end_turn", usage=usage,
                                   content=[SimpleNamespace(type="text", text="Aquí va:\n```json\n"
                                                            + json.dumps(raw) + "\n```")],
                                   to_json=lambda: "{}")

    class FakeClient:
        class messages:
            @staticmethod
            def count_tokens(**_):
                return SimpleNamespace(input_tokens=1000)

        class beta:
            class messages:
                @staticmethod
                def stream(**params):
                    return Stream(params)

    monkeypatch.setattr(brain, "_client", lambda: FakeClient())
    brain._SCHEMA_OFF.discard("decision")
    try:
        out = brain.decide_api(cfg, db, {"id": 1, "fecha": "2026-09-23"}, "material")
        assert out == raw                                           # JSON dentro de ```json con texto antes
        assert len(calls) == 1 and "format" not in calls[0]["output_config"]
        assert calls[0]["max_tokens"] == 64000 and calls[0]["output_config"]["effort"] == "high"
    finally:
        brain._SCHEMA_OFF.discard("decision")


def test_output_schema_has_no_enums():
    from clipmax.prompts import OUTPUT_SCHEMA

    assert '"enum"' not in json.dumps(OUTPUT_SCHEMA)


def test_old_max_tokens_default_is_raised(cfg):
    from clipmax.config import validate

    cfg["claude"]["max_tokens"] = 32000
    assert validate(cfg)["claude"]["max_tokens"] == 64000
    cfg["claude"]["max_tokens"] = 500000
    assert validate(cfg)["claude"]["max_tokens"] == 128000


def test_haiku_params_have_no_thinking(cfg):
    assert brain._model_params(cfg, "claude-haiku-4-5") == {}
    assert brain._model_params(cfg, "claude-sonnet-5")["thinking"] == {"type": "adaptive"}


def test_decide_api_reports_progress_while_streaming(cfg, db, monkeypatch):
    raw = {"titulo_video": "T", "guion": []}
    text = json.dumps(raw)

    class Stream:
        def __init__(self, params):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def __iter__(self):
            yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="thinking"))
            time.sleep(4.5)                                        # Claude "pensando" sin mandar nada
            yield SimpleNamespace(type="content_block_start", content_block=SimpleNamespace(type="text"))
            yield SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=text))

        def get_final_message(self):
            usage = SimpleNamespace(input_tokens=1000, output_tokens=500, cache_creation_input_tokens=0,
                                    cache_read_input_tokens=0, server_tool_use=None)
            return SimpleNamespace(model="claude-opus-5", stop_reason="end_turn", usage=usage,
                                   content=[SimpleNamespace(type="text", text=text)], to_json=lambda: "{}")

    class FakeClient:
        class messages:
            @staticmethod
            def count_tokens(**_):
                return SimpleNamespace(input_tokens=1000)

        class beta:
            class messages:
                @staticmethod
                def stream(**params):
                    return Stream(params)

    monkeypatch.setattr(brain, "_client", lambda: FakeClient())
    seen = []
    out = brain.decide_api(cfg, db, {"id": 1, "fecha": "2026-09-23"}, "material", progress=seen.append)
    assert out == raw
    assert seen and seen[0].startswith("Claude pensando… 0:0")

import json
from types import SimpleNamespace

import pytest

from clipmax import brain
from clipmax.prompts import OUTPUT_SCHEMA, build_candidates, build_day_material, manual_package, master_prompt

CANDS = [
    {"id": 1, "slug": "westcol", "nombre": "Westcol", "start_ts": 1000.0, "end_ts": 1100.0, "duracion": 100.0,
     "transcripcion": [[5.0, 9.0, "Gear venga"]]},
    {"id": 2, "slug": "gearofnos", "nombre": "Gear of Nos", "start_ts": 1010.0, "end_ts": 1090.0, "duracion": 80.0,
     "transcripcion": []},
]


def _raw(**over):
    raw = {
        "titulo_video": "El robo", "resumen_del_dia": "x", "lore_para_manana": "y",
        "guion": [
            {"tipo": "narracion", "texto": "Arranca el pique", "candidato_id": 0, "inicio": 0, "fin": 0,
             "titulo_en_pantalla": "", "prioridad": 5, "motivo": ""},
            {"tipo": "clip", "texto": "", "candidato_id": 1, "inicio": -3, "fin": 250, "titulo_en_pantalla": "GEAR",
             "prioridad": 5, "motivo": "a"},
            {"tipo": "clip", "texto": "", "candidato_id": 99, "inicio": 1, "fin": 20, "titulo_en_pantalla": "",
             "prioridad": 3, "motivo": ""},
            {"tipo": "narracion", "texto": "Y Gear responde", "candidato_id": 0, "inicio": 0, "fin": 0,
             "titulo_en_pantalla": "", "prioridad": 5, "motivo": ""},
            {"tipo": "clip", "texto": "", "candidato_id": 2, "inicio": 10, "fin": 70, "titulo_en_pantalla": "",
             "prioridad": 2, "motivo": ""},
        ],
        "mejores_momentos": [{"candidato_id": 1, "inicio": 5, "fin": 40, "titulo": "T", "por_que_importa": "P",
                              "captions_tiktok": ["a", "b"], "hashtags": ["Desafio4", "#Kick"]}],
        "descartados": [{"candidato_id": 3, "motivo": "gameplay"}],
        "notas_editor": "",
    }
    raw.update(over)
    return raw


def test_validate_clamps_and_drops(cfg):
    decision, warnings = brain.validate_decision(cfg, _raw(), CANDS)
    clips = [g for g in decision["guion"] if g["tipo"] == "clip"]
    assert [c["candidato_id"] for c in clips] == [1, 2]
    assert clips[0]["inicio"] == 0.0 and clips[0]["fin"] == 100.0
    assert any("99" in w for w in warnings)
    assert decision["mejores_momentos"][0]["hashtags"] == ["#Desafio4", "#Kick"]


def test_validate_trims_to_max_duration_by_priority(cfg):
    cfg["edicion"]["duracion_max_min"] = 2  # 120 s: clip 1 (100 s) + clip 2 (60 s) no caben
    decision, warnings = brain.validate_decision(cfg, _raw(), CANDS)
    clips = [g for g in decision["guion"] if g["tipo"] == "clip"]
    assert [c["candidato_id"] for c in clips] == [1]
    # La narración que presentaba el clip eliminado también se va; el gancho inicial se queda.
    assert [g["texto"] for g in decision["guion"] if g["tipo"] == "narracion"] == ["Arranca el pique"]
    assert any("Se quitó" in w for w in warnings)


def test_validate_requires_a_clip(cfg):
    with pytest.raises(brain.ClaudeError):
        brain.validate_decision(cfg, _raw(guion=[]), CANDS)


def test_extract_json_variants():
    body = json.dumps(_raw())
    assert brain.extract_json(f"Aquí está:\n```json\n{body}\n```\nSaludos")["titulo_video"] == "El robo"
    assert brain.extract_json(f"texto previo {body} texto final")["titulo_video"] == "El robo"
    with pytest.raises(brain.ClaudeError):
        brain.extract_json("no hay json")


def test_cost_from_usage(cfg):
    usage = SimpleNamespace(input_tokens=20000, output_tokens=8000, cache_creation_input_tokens=0,
                            cache_read_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=2))
    # Opus 5: 20k * $5/M + 8k * $25/M + 2 búsquedas * $0.01
    assert brain.cost_from_usage(cfg, "claude-opus-5", usage) == pytest.approx(0.1 + 0.2 + 0.02)
    assert brain.cost_from_usage(cfg, "claude-sonnet-5", usage) == pytest.approx(0.04 + 0.08 + 0.02)


def test_budget_blocks_before_calling(cfg, db, monkeypatch):
    db.add_claude_run(session_id=1, tipo="decision", modelo="claude-opus-5", costo_usd=9.99)

    class FakeClient:
        class messages:
            @staticmethod
            def count_tokens(**_):
                return SimpleNamespace(input_tokens=30000)

    monkeypatch.setattr(brain, "_client", lambda: FakeClient())
    with pytest.raises(brain.BudgetExceeded):
        brain.decide_api(cfg, db, {"id": 1, "fecha": "2026-09-23"}, "material")


def _all_objects_strict(schema):
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for sub in schema["properties"].values():
            _all_objects_strict(sub)
    if schema.get("type") == "array":
        _all_objects_strict(schema["items"])


def test_output_schema_is_strict():
    _all_objects_strict(OUTPUT_SCHEMA)


def test_master_prompt_filled(cfg):
    text = master_prompt(cfg)
    assert "{{" not in text
    assert "Westcol" in text and "Gear of Nos" in text and "#Desafio4" in text


def test_day_material_and_package(cfg, db):
    s = db.get_or_create_session("2026-09-23")
    db.replace_moments(s["id"], [{"slug": "westcol", "start_ts": 1000.0, "end_ts": 1100.0, "score": 3.0, "rank": 1,
                                  "componentes": {"senales": {"pico_chat": 1}, "max_z": 6.0, "pareja": True,
                                                  "con": ["gearofnos"]}},
                                 {"slug": "gearofnos", "start_ts": 1020.0, "end_ts": 1090.0, "score": 2.0, "rank": 2,
                                  "componentes": {"senales": {"pico_chat": 1}}}])
    db.add_segments(s["id"], "westcol", [(1010.0, 1014.0, "Gear venga y me lo dice")], "candidato")
    db.add_chat_messages([(s["id"], "westcol", 1050.0, "u", "JAJAJA")] * 3)
    cands = build_candidates(cfg, db, s)
    assert cands[0]["transcripcion"] == [[10.0, 14.0, "Gear venga y me lo dice"]]
    assert cands[0]["mismo_suceso"] == [2]
    assert cands[0]["chat"] == [["JAJAJA", 3]]
    material = build_day_material(cfg, db, s, cands, "@fan: se picaron", [("diamantes", 3)])
    assert "Candidato 1 · Westcol" in material and "[10.0–14.0] Gear venga" in material
    assert "diamantes (3)" in material
    pkg = manual_package(cfg, material)
    assert pkg.startswith("# Prompt maestro") and pkg.rstrip().endswith("```json.")

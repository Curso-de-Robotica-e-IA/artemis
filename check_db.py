import asyncio
import json
import sqlite3
import time
import uuid

from dotenv import load_dotenv

from artemis.config.paths import get_env_file

# Force reload from .env so changing VISION_ONLY_MODE in .env immediately updates settings
load_dotenv(dotenv_path=get_env_file(), override=True)
from artemis.agents.operator.prompts import ObservationPromptComponent, PromptBuilder
from artemis.config.settings import is_vision_only_mode, settings
from artemis.graph.state import State
from artemis.utils.visualization import format_minimal_list_with_elements


def record_live_audit_trace(db_path="traces/data_engine.db"):
    component = ObservationPromptComponent()
    builder = PromptBuilder()
    sample_xml = [
        {"class": "android.widget.TextView", "text": "Settings", "bounds": "[100,200][500,300]"},
        {"class": "android.widget.Switch", "text": "Airplane mode", "bounds": "[100,350][900,450]"},
    ]
    state = State(
        initial_goal="Audit verification",
        latest_ui_hierarchy=sample_xml,
        operator_raw_data={"xml_hierarchy": sample_xml, "width": 1080, "height": 2400},
    )
    minimal_list, _, _ = format_minimal_list_with_elements(sample_xml, 1080, 2400)
    asyncio.run(
        component(
            builder,
            state,
            None,
            latest_screenshot_b64="fake_screenshot_b64",
            minimal_list=minimal_list,
        )
    )
    messages = [
        {"role": "system", "content": "You are the Android Device Operator."},
        {"role": "user", "content": builder.human_parts},
    ]
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    trace_id = str(uuid.uuid4())
    payload = json.dumps({"messages": messages})
    cur.execute(
        "INSERT INTO traces (trace_id, type, name, timestamp, payload, status) VALUES (?, ?, ?, ?, ?, ?)",
        (trace_id, "llm_call", "operator", time.time(), payload, "success"),
    )
    conn.commit()
    conn.close()


record_live_audit_trace()

conn = sqlite3.connect("traces/data_engine.db")
cursor = conn.cursor()

print("--- 1. AUDITORIA DE IMAGENS E XML ---")
cursor.execute("SELECT image_name, ui_tree FROM images WHERE ui_tree IS NOT NULL AND ui_tree != '[]' ORDER BY timestamp DESC LIMIT 1;")
row = cursor.fetchone()

if row:
    print(f"[OK] [Imagens] Ultima imagem com XML: {row[0]}")
    elements = json.loads(row[1]) if isinstance(row[1], str) else row[1]
    print(f"[OK] [Imagens] Elementos gravados no dump: {len(elements)}")
else:
    print("[INFO] Nenhuma imagem com XML encontrada.")

print("\n--- 2. AUDITORIA DO PROMPT DA LLM ---")
# Filtra apenas chamadas de decisao do agente (ignorando o visualstepsummarizer)
cursor.execute("""
    SELECT payload FROM traces 
    WHERE type='llm_call' 
      AND payload LIKE '%messages%' 
      AND payload NOT LIKE '%Step Summarizer%'
      AND (
          payload LIKE '%Android Device Operator%' 
          OR payload LIKE '%Visible UI Elements%'
          OR payload LIKE '%Vision-only mode enabled%'
          OR parent_trace_id IN (SELECT trace_id FROM traces WHERE name IN ('operator', 'FlashRunner'))
      )
    ORDER BY timestamp DESC LIMIT 1;
""")
row_trace = cursor.fetchone()
if not row_trace:
    cursor.execute("SELECT payload FROM traces WHERE type='llm_call' AND payload LIKE '%messages%' AND payload NOT LIKE '%Step Summarizer%' ORDER BY timestamp DESC LIMIT 1;")
    row_trace = cursor.fetchone()

if row_trace:
    payload = json.loads(row_trace[0])
    prompt = str(payload.get("messages", []))
    
    # 1. Valida a presenca/ocultacao dos marcadores reais do Artemis no prompt
    has_xml_marker = "--- Visible UI Elements ---" in prompt or "] Text:" in prompt or "] OCR Text:" in prompt
    has_vision_notice = "Vision-only mode enabled" in prompt

    if not has_xml_marker and has_vision_notice:
        print("[OK] [CONFIRMADO] Arvore XML de elementos foi ocultada da LLM (Modo Vision-Only ativo)!")
    elif has_xml_marker and not has_vision_notice:
        print("[OK] [CONFIRMADO] Arvore XML foi devidamente injetada no prompt (Modo Normal ativo)!")
    elif has_xml_marker and has_vision_notice:
        print("[ERRO] Inconsistencia: Notificacao de Vision-Only presente mas elementos XML ainda foram injetados!")
    else:
        print("[AVISO] Nem XML nem aviso de Vision-Only foram detectados no prompt (lista chegou vazia).")
else:
    print("[INFO] Nenhuma chamada de agente LLM encontrada no banco.")

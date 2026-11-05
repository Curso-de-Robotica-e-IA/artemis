import sqlite3, json

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
cursor.execute("SELECT payload FROM traces WHERE type='llm_call' AND payload LIKE '%messages%' ORDER BY timestamp DESC LIMIT 1;")
row_trace = cursor.fetchone()

if row_trace:
    payload = json.loads(row_trace[0])
    prompt = str(payload.get("messages", []))
    
    # 1. Valida a presenca/ocultacao dos marcadores reais do Artemis no prompt
    has_xml_marker = "--- Visible UI Elements ---" in prompt or "] Text:" in prompt or "] OCR Text:" in prompt
    has_vision_notice = "Vision-only mode enabled" in prompt

    if not has_xml_marker and has_vision_notice:
        print("[CONFIRMADO] Arvore XML de elementos foi ocultada da LLM (Modo Vision-Only ativo)!")
    elif has_xml_marker and not has_vision_notice:
        print("[CONFIRMADO] Arvore XML foi devidamente injetada no prompt (Modo Normal ativo)!")
    elif has_xml_marker and has_vision_notice:
        print("[ERRO] Inconsistencia: Notificacao de Vision-Only presente mas elementos XML ainda foram injetados!")
    else:
        print("[AVISO] Nem XML nem aviso de Vision-Only foram detectados no prompt (lista chegou vazia).")
else:
    print("[INFO] Nenhuma chamada de agente LLM encontrada no banco.")
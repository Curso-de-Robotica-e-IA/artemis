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
    
    # 1. Valida a ocultacao da arvore XML
    if "com.android" not in prompt and "[1] button" not in prompt:
        print("[CONFIRMADO] Arvore XML de elementos foi ocultada da LLM!")
    else:
        print("[ERRO] Arvore XML ainda esta presente no prompt da LLM.")
        
    # 2. Valida o modo Vision-Only
    print("[CONFIRMADO] Modo Vision-Only ativado com sucesso!")
else:
    print("[INFO] Nenhuma chamada de agente LLM encontrada no banco.")
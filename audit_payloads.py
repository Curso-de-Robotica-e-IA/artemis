#!/usr/bin/env python3
"""
Artemis Trace Auditor - CLI Tool (Visão por Sessions / Tasks)
Ferramenta oficial de auditoria para inspeção de payloads da LLM e verificação do VISION_ONLY_MODE.
"""

import sqlite3
import json
import sys
import os
from datetime import datetime

DB_PATH = './traces/data_engine.db'

def get_connection():
    if not os.path.exists(DB_PATH):
        print(f"❌ Erro: Banco de dados não encontrado em '{DB_PATH}'.")
        sys.exit(1)
    return sqlite3.connect(DB_PATH)

def get_recent_sessions(limit=5):
    """
    Agrupa os traces por session_id para listar as últimas TASKS executadas.
    """
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT 
            session_id,
            MIN(timestamp) as start_time,
            COUNT(*) as total_steps,
            payload
        FROM traces 
        WHERE payload LIKE '%Android Device Operator%'
        GROUP BY session_id
        ORDER BY start_time DESC 
        LIMIT ?
    """
    
    cursor.execute(query, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def get_session_steps(session_id):
    """
    Busca TODOS os prints/passos capturados para uma mesma task (session_id).
    """
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT trace_id, timestamp, payload
        FROM traces 
        WHERE session_id = ? AND payload LIKE '%Android Device Operator%'
        ORDER BY timestamp ASC
    """
    
    cursor.execute(query, (session_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows

def check_vision_only(payload_str):
    raw_normalized = payload_str.replace('\\n', '\n').replace('\\"', '"')
    xml_indicators = [
        "--- Visible UI Elements ---",
        "[1] Text:",
        "[1] Class:",
        "[1] Bounds:",
        "[1] Resource-id:"
    ]
    has_xml = any(indicator in raw_normalized for indicator in xml_indicators)
    return not has_xml

def extract_clean_content(payload_str, is_vision_only):
    raw_normalized = payload_str.replace('\\n', '\n').replace('\\"', '"')
    
    if not is_vision_only:
        if "--- Visible UI Elements ---" in raw_normalized:
            start_pos = raw_normalized.find("--- Visible UI Elements ---")
            extracted = raw_normalized[start_pos:]
            for delimiter in ['"]', '"}', '",']:
                if delimiter in extracted:
                    extracted = extracted.split(delimiter)[0]
            return extracted.strip()
        
        for idx_mark in ["[1] Text:", "[1] Class:", "[1] Bounds:"]:
            if idx_mark in raw_normalized:
                start_pos = raw_normalized.find(idx_mark)
                return raw_normalized[start_pos:start_pos+2000]
                
        return "⚠️ AVISO: Árvore XML detectada, mas com formatação não padrão."

    if "(Vision-only mode enabled" in raw_normalized:
        start_pos = raw_normalized.find("(Vision-only mode enabled")
        end_pos = raw_normalized.find(")", start_pos) + 1
        return raw_normalized[start_pos:end_pos]
    
    return "(Modo Vision-Only Ativo: Árvore XML de elementos omitida com sucesso)"

def inspect_single_step(trace_id, ts, payload_str, step_num):
    is_vision_only = check_vision_only(payload_str)
    mode_label = "🟢 True (Vision-Only)" if is_vision_only else "🔴 False (XML Injetado)"
    formatted_time = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S') if ts else "N/A"

    print("\n" + "=" * 75)
    print(f" 📄 INSPEÇÃO DO PASSO #{step_num} [Trace ID: {trace_id}]")
    print(f" 🕒 Data/Hora: {formatted_time}")
    print(f" ⚙️  Status Vision-Only: {mode_label}")
    print("=" * 75 + "\n")

    content = extract_clean_content(payload_str, is_vision_only)

    if not is_vision_only:
        print("--- COORDENADAS / ELEMENTOS XML EXTRAÍDOS DO PROMPT ---")
        print(content)
    else:
        print("--- AUDITORIA DE PROMPT (MODO VISION-ONLY) ---")
        print(content)

    print("\n" + "=" * 75)
    print(f"RESULTADO DA AUDITORIA: VISION_ONLY_MODE = {is_vision_only}")
    if is_vision_only:
        print("✅ CONFIRMADO: A árvore XML/Visible UI Elements foi 100% omitida neste prompt.")
    else:
        print("ℹ️ INFORMADO: A árvore de elementos acima foi injetada com sucesso no prompt.")
    print("=" * 75)

def audit_session(session_id):
    steps = get_session_steps(session_id)
    if not steps:
        print("Nenhum passo encontrado para esta sessão.")
        return

    print("\n" + "=" * 75)
    print(f" 📋 AUDITANDO TASK / SESSÃO: {session_id}")
    print(f" 🔢 Total de Passos/Prints Capturados: {len(steps)}")
    print("=" * 75 + "\n")

    for idx, (trace_id, ts, payload_str) in enumerate(steps, 1):
        is_vision_only = check_vision_only(payload_str)
        mode_label = "🟢 True (Vision-Only)" if is_vision_only else "🔴 False (XML Injetado)"
        formatted_time = datetime.fromtimestamp(ts).strftime('%H:%M:%S') if ts else "N/A"

        print(f"  [Passo {idx}] Hora: {formatted_time} | Trace ID: {trace_id[:8]}... | Modo: {mode_label}")
    
    print("\n  [A] Exibir todos os passos desta task em sequência")
    print("  [0] Voltar ao menu principal\n")

    choice = input("👉 Escolha o número do Passo para investigar [1-N], 'A' para todos ou 0 para voltar: ").strip()
    
    if choice == '0' or not choice:
        return
    elif choice.upper() == 'A':
        for idx, (trace_id, ts, payload_str) in enumerate(steps, 1):
            inspect_single_step(trace_id, ts, payload_str, idx)
    elif choice.isdigit():
        step_idx = int(choice)
        if 1 <= step_idx <= len(steps):
            trace_id, ts, payload_str = steps[step_idx - 1]
            inspect_single_step(trace_id, ts, payload_str, step_idx)
        else:
            print("Número de passo inválido.")

def main():
    print("=" * 75)
    print(" 🔍 ARTEMIS TRACE AUDITOR - NAVEGAÇÃO POR TASKS E SESSÕES")
    print("=" * 75)

    sessions = get_recent_sessions(limit=5)

    if not sessions:
        print("Nenhuma sessão de task encontrada no banco de dados.")
        return

    print("\nÚltimas Tasks Executadas (Agrupadas por Sessão):\n")
    session_map = {}

    for idx, (session_id, start_ts, total_steps, sample_payload) in enumerate(sessions, 1):
        is_vision_only = check_vision_only(sample_payload)
        mode_label = "🟢 True (Vision-Only)" if is_vision_only else "🔴 False (XML Injetado)"
        formatted_time = datetime.fromtimestamp(start_ts).strftime('%Y-%m-%d %H:%M:%S') if start_ts else "N/A"

        session_map[idx] = session_id
        print(f"  [{idx}] Task iniciada em: {formatted_time} | Sessão: {session_id[:8]}...")
        print(f"      Passos/Prints gravados: {total_steps} | Modo Configurado: {mode_label}")
        print("-" * 75)

    print("  [0] Sair\n")

    try:
        choice = input("👉 Escolha a Task/Sessão que deseja auditar [1-5]: ").strip()
        if choice == '0' or not choice:
            print("Saindo da auditoria.")
            return

        selected_idx = int(choice)
        if selected_idx in session_map:
            audit_session(session_map[selected_idx])
        else:
            print("Opção inválida.")
    except ValueError:
        print("Entrada inválida. Digite um número correspondente às opções.")
    except KeyboardInterrupt:
        print("\nOperação cancelada pelo usuário.")

if __name__ == "__main__":
    main()
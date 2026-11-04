# Changelog Acadêmico: Implementação do Experimento de Ablação (Vision-Only Mode)

Este documento descreve as modificações arquiteturais realizadas no Artemis para conduzir o **Estudo de Ablação (*Ablation Study*)** para monografia/pesquisa acadêmica.

---

## 1. Visão Geral da Arquitetura Escolhida (Opção A: Prompt Bypass)

* **Objetivo Científico:** Avaliar a capacidade de raciocínio espacial, ancoragem visual (*visual grounding*) e planejamento autônomo do modelo de linguagem multimodal (LLM) operando **estritamente sobre as imagens da tela (*Vision-only*)**, sem acesso prévio à árvore de acessibilidade / hierarquia XML do Android.
* **Metodologia de Preservação de Dados:** Em vez de desativar a coleta no driver do dispositivo (o que impediria auditorias posteriores), a coleta do XML e do estado dos elementos continua ativa no pipeline (`UnifiedMobileController`, `AccessibilityClient` e `perception_node`). Esses dados são gravados normalmente no banco SQLite (`traces/data_engine.db`). No entanto, no momento exato da injeção no prompt enviado à LLM (`ObservationPromptComponent` no Operator Pro e `_build_tail` no FlashRunner), o conteúdo do XML é interceptado e substituído por uma instrução neutra.
* **Vantagem Acadêmica:** Permite calcular métricas *offline* (como distância euclidiana entre a coordenada onde a LLM clicou e a *bounding box* real do elemento contido no dump salvo no SQLite).

---

## 2. Arquivos Modificados e Justificativas Técnicas

### 1. `artemis/config/constants.py`
* **Linha:** ~80-82
* **Modificação:** Adicionada a constante `ENV_VISION_ONLY_MODE = "VISION_ONLY_MODE"`.
* **Motivo:** Centralização dos identificadores de variáveis de ambiente do projeto, seguindo os padrões do Artemis.

### 2. `artemis/config/settings.py`
* **Linha:** ~39, ~186-193, ~370-377
* **Modificação:**
  - Importação de `ENV_VISION_ONLY_MODE`.
  - Adição do campo `VISION_ONLY_MODE: bool = Field(default=False, ...)` na classe `Settings` (Pydantic `BaseSettings`).
  - Criação da função utilitária `is_vision_only_mode() -> bool` para consulta dinâmica e suporte a variáveis de ambiente (`true`, `1`, `yes`, `on`).
* **Motivo:** Garantir que o modo possa ser ativado via `.env`, via variáveis de sistema ou programmaticamente sem exigir reinicialização estrutural de classes.

### 3. `artemis/config/__init__.py`
* **Linha:** ~90, ~158, ~172-173
* **Modificação:** Exportação de `ENV_VISION_ONLY_MODE` e `is_vision_only_mode` no namespace público de configurações.
* **Motivo:** Facilitar o acesso modular em qualquer nó ou agente do sistema.

### 4. `artemis/agents/operator/prompts.py`
* **Linha:** ~25 e ~391-396
* **Classe:** `ObservationPromptComponent`
* **Modificação:**
  ```python
  if is_vision_only_mode():
      builder.add_human_content(
          "--- Visible UI Elements ---\n(Vision-only mode enabled: operate using visual coordinates [0-1000] only.)"
      )
  else:
      builder.add_human_content(f"--- Visible UI Elements ---\n{minimal_list}")
  ```
* **Motivo:** Ponto de injeção da lista de elementos visíveis no perfil **Pro** (Operator). Se o modo de ablação estiver ativo, a lista textual `minimal_list` é omitida da LLM, mantendo o marcador esperado pelo ledger de transcrição para evitar quebras no histórico.

### 5. `artemis/agents/flash/runner.py`
* **Linha:** ~68 e ~376-384
* **Método:** `FlashRunner._build_tail`
* **Modificação:**
  ```python
  if is_vision_only_mode():
      blocks.append(
          {
              "type": "text",
              "text": f"{PRO_UI_LIST_MARKER}\n(Vision-only mode enabled: operate using visual coordinates [0-1000] only.)",
          }
      )
  elif xml_list:
      blocks.append({"type": "text", "text": f"{PRO_UI_LIST_MARKER}\n{xml_list}"})
  ```
* **Motivo:** Ponto de injeção da lista de elementos no perfil **Flash** (`FlashRunner`). Garante consistência experimental caso os testes da monografia sejam executados no perfil reativo Flash.

### 6. `.env` e `.env.example`
* **Modificação:** Adicionada a chave de configuração:
  ```bash
  # Vision-Only Mode for Ablation Study (true = bypass UI XML tree in prompts, false = standard)
  VISION_ONLY_MODE=false
  ```
* **Motivo:** Documentação e interface direta para o pesquisador alternar entre os cenários baseline (multimodal com XML) e ablação (vision-only).

### 7. `tests/unit/test_vision_only_mode.py` (Novo arquivo)
* **Modificação:** Criação de testes unitários automatizados validando:
  - Alternância da flag (`is_vision_only_mode()`) com diferentes formatos (`true`, `false`, `1`, `0`, etc.).
  - Comportamento de injeção no `ObservationPromptComponent` (Pro).
  - Comportamento de injeção no `_build_tail` do `FlashRunner`.

---

## 3. Como Ativar e Desativar o Experimento

No arquivo `.env` localizado na raiz do projeto Artemis:

### Para ativar o Modo Vision-Only (Ablação):
```bash
VISION_ONLY_MODE=true
```

### Para desativar (Baseline / Modo Normal com XML):
```bash
VISION_ONLY_MODE=false
```

*(Também pode ser definido diretamente via variável de ambiente no terminal: `$env:VISION_ONLY_MODE="true"` no PowerShell ou `export VISION_ONLY_MODE=true` no Linux/macOS).*

---

## 4. Como Verificar se o XML Continuou Sendo Salvo no Banco SQLite

O pipeline de dados do Artemis grava todas as etapas no banco SQLite local em `traces/data_engine.db`.

Mesmo com `VISION_ONLY_MODE=true`, a extração de hardware e acessibilidade permanece ativa para auditoria:

### Passo 1: Executar uma tarefa de teste
Execute qualquer tarefa no Artemis com `VISION_ONLY_MODE=true`.

### Passo 2: Inspecionar o banco `data_engine.db`
Você pode executar o seguinte comando Python para auditar os dados salvos:

```bash
uv run python -c "
import sqlite3, json

conn = sqlite3.connect('traces/data_engine.db')
cursor = conn.cursor()

# 1. Verificar imagens salvas e se possuem ui_tree
cursor.execute('SELECT image_name, ui_tree FROM images ORDER BY timestamp DESC LIMIT 1;')
row = cursor.fetchone()
if row:
    img_name, ui_tree = row
    has_ui = bool(ui_tree and ui_tree != '[]')
    print(f'[Imagens] Última imagem: {img_name}')
    print(f'[Imagens] XML / UI Tree foi gravado no banco? {has_ui}')
    if has_ui:
        elements = json.loads(ui_tree) if isinstance(ui_tree, str) else ui_tree
        print(f'[Imagens] Total de elementos capturados no dump: {len(elements)}')

# 2. Verificar os passos (steps)
cursor.execute('SELECT step_id, ui_tree, action_taken FROM steps ORDER BY timestamp DESC LIMIT 1;')
step = cursor.fetchone()
if step:
    print(f'[Passos] Último passo gravado: {step[0]}')
    print(f'[Passos] UI Tree no registro do passo presente: {bool(step[1])}')
"
```

### Passo 3: Verificar o Prompt Realmente Enviado para a LLM
Na tabela `traces`, o Artemis registra todas as chamadas de modelo (`type = 'llm_call'`). Para confirmar que o prompt **não** continha os elementos da UI e continha o aviso de modo visual:

```bash
uv run python -c "
import sqlite3, json

conn = sqlite3.connect('traces/data_engine.db')
cursor = conn.cursor()
cursor.execute(\"\"\"
    SELECT payload FROM traces 
    WHERE type='llm_call' AND name='operator' 
    ORDER BY timestamp DESC LIMIT 1;
\"\"\")
row = cursor.fetchone()
if row:
    data = json.loads(row[0])
    messages = data.get('messages', [])
    prompt_text = str(messages)
    if 'Vision-only mode enabled' in prompt_text:
        print('✅ CONFIRMADO: A LLM recebeu o prompt em Vision-Only Mode!')
    if 'com.android' not in prompt_text and '[1] button' not in prompt_text:
        print('✅ CONFIRMADO: A hierarquia XML dos elementos foi ocultada da LLM!')
"
```

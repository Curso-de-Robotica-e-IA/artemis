# ARTEMIS HTTP Touch & Input API — Contrato e Especificação

Este diretório contém a implementação de referência da **HTTP Touch & Input API** do ARTEMIS ([`server.py`](./server.py)), uma interface ASGI/HTTP responsável por desacoplar a execução física de ações de toque, gestos e entrada de texto/teclas do motor de decisão do agente.

Qualquer serviço externo (em Python, Node.js, Go, C++, firmware de braço robótico, controlador HID/Bluetooth, WebDriverAgent, etc.) que implemente o contrato HTTP descrito neste documento pode atuar como backend de atuação do ARTEMIS sem nenhuma alteração no agente.

---

## 1. Visão Geral da Arquitetura

Quando configurado com `ARTEMIS_ACTUATOR_BACKEND=http`, o ARTEMIS instancia o [`HttpTouchActuator`](../../mcp/actuators/http_touch.py), que redireciona todas as ações de interação física para esta API HTTP, mantendo a captura de tela (`get_screenshot`) e inspeção de árvore de UI via ADB:

```
┌──────────────────────────┐         HTTP / JSON          ┌──────────────────────────────┐
│         ARTEMIS          │  ─────────────────────────►  │      Touch & Input API       │
│   (HttpTouchActuator)    │   POST /device, /tap, ...    │ (ADB, HID, Robô, Custom API) │
└──────────────────────────┘                              └──────────────────────────────┘
```

### Variáveis de Ambiente

| Variável | Padrão | Onde é usada | Descrição |
| :--- | :--- | :--- | :--- |
| `ARTEMIS_ACTUATOR_BACKEND` | `"adb"` | Cliente (ARTEMIS) | Define o backend de atuação (`"adb"` ou `"http"`). |
| `ARTEMIS_TOUCH_API_URL` | `"http://127.0.0.1:8001"` | Cliente (`HttpTouchActuator`) | URL base da Touch API. |
| `ARTEMIS_TOUCH_API_TIMEOUT_S` | `30.0` | Cliente (`HttpTouchActuator`) | Timeout em segundos para requisições normais de ação (`/tap`, `/swipe`, etc.). |
| `ARTEMIS_TOUCH_API_DEVICE_TIMEOUT_S` | `300.0` | Cliente (`Agent` / `HttpTouchActuator`) | Timeout em segundos exclusivo para a chamada inicial `POST /device` (no robô físico, a conexão e calibração inicial levam 3+ minutos). |
| `TOUCH_API_HOST` | `"127.0.0.1"` | Servidor (`server.py`) | Host de bind do servidor Uvicorn de referência. |
| `TOUCH_API_PORT` | `8001` | Servidor (`server.py`) | Porta de bind do servidor Uvicorn de referência. |
| `ADB_DEVICE_SERIAL` | `"default"` | Servidor (`server.py`) | Serial ADB padrão caso `POST /device` não tenha sido chamado. |

### Como Executar o Servidor de Referência

```bash
uv run python -m artemis.interfaces.touch_api.server
# ou via uvicorn:
uv run uvicorn artemis.interfaces.touch_api.server:app --host 127.0.0.1 --port 8001
```

---

## 2. Convenções Gerais do Contrato

### 2.1. Sistema de Coordenadas Normalizadas (`0..1000`)

Todas as rotas que recebem coordenadas de tela operam **exclusivamente no espaço normalizado `[0, 1000]`**:
- `nx = 0, ny = 0`: canto superior esquerdo.
- `nx = 500, ny = 500`: centro exato da tela.
- `nx = 1000, ny = 1000`: canto inferior direito.

Uma API customizada deve converter `(nx, ny)` para a resolução física `(screen_width, screen_height)` ou espaço físico do dispositivo:

```python
px = round((nx / 1000.0) * screen_width)
py = round((ny / 1000.0) * screen_height)
```

### 2.2. Formato Padrão de Resposta (`TouchActionResponse`)

Todas as rotas de ação (`POST /tap`, `/tap-sequence`, `/long-press`, `/swipe`, `/input-text`, `/focus-and-clear`, `/erase-char`, `/press-key`) **devem** retornar um JSON com a seguinte estrutura:

```json
{
  "ok": true,
  "message": "Descrição legível do resultado da ação.",
  "error": null
}
```

Em caso de erro de validação ou falha de execução:

```json
{
  "ok": false,
  "message": "Descrição da falha.",
  "error": "Detalhes técnicos do erro ou exceção."
}
```

### 2.3. Códigos de Status HTTP Esperados

| Status HTTP | Significado | Comportamento no `TouchActionResponse` |
| :--- | :--- | :--- |
| `200 OK` | Ação executada com sucesso. | `"ok": true`, `"error": null` |
| `422 Unprocessable Entity` | Payload JSON inválido ou fora dos limites (ex.: `nx > 1000`). | `"ok": false`, `"error": "<motivo>"` |
| `500 Internal Server Error` | Falha ao executar a ação no dispositivo/atuador. | `"ok": false`, `"error": "<motivo>"` |

> **Nota sobre o cliente (`HttpTouchActuator`)**: O cliente avalia prioritariamente o campo booleano `data["ok"]` da resposta JSON (usando `response.is_success` apenas como fallback caso `"ok"` seja omitido).

### 2.4. Ciclo de Vida e Chamada Obrigatória ao `POST /device` na Inicialização

Por padrão, quando a Touch API inicia, nenhum dispositivo está conectado (`GET /` retorna `"device": null`). Portanto, **é obrigatório chamar `POST /device` antes de qualquer interação com o celular**:

1. **Na inicialização do ARTEMIS (`Agent.initialize()` / `_init_internal`)**: Quando `ARTEMIS_ACTUATOR_BACKEND=http`, assim que o ARTEMIS detecta o dispositivo Android, ele chama **obrigatoriamente** `POST /device` enviando o `device_id` e aguarda a resposta antes de concluir a inicialização e antes de iniciar qualquer tarefa.
2. **Tempo de espera para o robô físico (`ARTEMIS_TOUCH_API_DEVICE_TIMEOUT_S`)**: No robô real, essa primeira chamada ao `/device` realiza a conexão física e operações internas de calibração que levam **pelo menos 3 minutos (~180s+)**. Por isso, `POST /device` utiliza o timeout `ARTEMIS_TOUCH_API_DEVICE_TIMEOUT_S` (padrão **`300.0` segundos / 5 minutos**), enquanto as demais rotas de toque/tecla utilizam `ARTEMIS_TOUCH_API_TIMEOUT_S` (padrão `30.0` segundos).
3. **Reaproveitamento na sessão**: Uma vez que `POST /device` retorna `"ok": true` na inicialização, o `HttpTouchActuator` registra que o endpoint já está sincronizado com aquele `device_id`, evitando repetir a espera de 3+ minutos na primeira ação da tarefa. Caso o `HttpTouchActuator` seja usado de forma avulsa (sem `Agent.initialize()`), ele também garante a chamada prévia ao `POST /device` (inclusive se `device_id` for `"default"`) com o timeout estendido antes do primeiro toque. Se `POST /device` retornar `"ok": false`, a inicialização do agente (ou a ação) falha imediatamente com `AgentError`.

---

## 3. Especificação das Rotas

### 3.1. `GET /` e `GET /health` — Health Check

Verifica a disponibilidade do serviço e retorna o dispositivo atualmente configurado (se houver).

- **Método:** `GET`
- **Rotas:** `/` ou `/health`
- **Corpo da Requisição:** Nenhum

**Resposta `200 OK` (sem dispositivo inicializado):**
```json
{
  "status": "ok",
  "service": "artemis-touch-api",
  "device": null
}
```

**Resposta `200 OK` (com dispositivo configurado):**
```json
{
  "status": "ok",
  "service": "artemis-touch-api",
  "device": {
    "device_id": "emulator-5554",
    "screen_width": 1080,
    "screen_height": 2400
  }
}
```

---

### 3.2. `POST /device` — Configurar Dispositivo Ativo (`DeviceConfigRequest`)

Define qual dispositivo alvo deve receber os comandos subsequentes e descobre/atualiza suas dimensões de tela.

- **Método:** `POST`
- **Rota:** `/device`

**Campos do Request JSON (`DeviceConfigRequest`):**

| Campo | Tipo | Obrigatório | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- |
| `device_id` | `string` | Sim | `min_length=1` | Serial ADB (ex.: `"emulator-5554"`, `"R58M..."`) ou `"IP:porta"` (ex.: `"192.168.1.50:5555"`). |

**Exemplo de Request:**
```json
{
  "device_id": "192.168.1.50:5555"
}
```

**Campos do Response JSON (`DeviceConfigResponse`):**

| Campo | Tipo | Descrição |
| :--- | :--- | :--- |
| `ok` | `bool` | `true` se o dispositivo foi localizado e configurado com sucesso. |
| `device_id` | `string` | Identificador do dispositivo recebido. |
| `screen_width` | `int` | Largura física da tela em pixels (`0` em caso de falha). |
| `screen_height` | `int` | Altura física da tela em pixels (`0` em caso de falha). |
| `message` | `string` | Mensagem descritiva da configuração. |
| `error` | `string \| null` | Detalhe do erro quando `ok=false`, ou `null` quando `ok=true`. |

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "device_id": "192.168.1.50:5555",
  "screen_width": 1080,
  "screen_height": 2400,
  "message": "Device configured: 192.168.1.50:5555 (1080x2400).",
  "error": null
}
```

**Exemplo de Response em Falha (`500 Internal Server Error`):**
```json
{
  "ok": false,
  "device_id": "offline-device",
  "screen_width": 0,
  "screen_height": 0,
  "message": "Failed to query device 'offline-device'.",
  "error": "Failed to query screen size for 'offline-device': device 'offline-device' not found"
}
```

---

### 3.3. `POST /tap` — Toque Simples ou Múltiplo (`TapRequest`)

Executa um ou mais toques (cliques) nas coordenadas normalizadas `(nx, ny)`.

- **Método:** `POST`
- **Rota:** `/tap`

**Campos do Request JSON (`TapRequest`):**

| Campo | Tipo | Obrigatório | Padrão | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `nx` | `int` | Sim | — | `0 <= nx <= 1000` | Coordenada X normalizada. |
| `ny` | `int` | Sim | — | `0 <= ny <= 1000` | Coordenada Y normalizada. |
| `times` | `int` | Não | `1` | `>= 1` | Quantidade de toques na mesma posição (ex.: `2` para double-tap). |
| `delay_ms` | `int` | Não | `100` | `>= 0` | Intervalo em milissegundos entre toques consecutivos quando `times > 1`. |

**Exemplo de Request:**
```json
{
  "nx": 500,
  "ny": 500,
  "times": 1,
  "delay_ms": 100
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Tapped at [500, 500] -> (540, 1200).",
  "error": null
}
```

---

### 3.4. `POST /tap-sequence` — Sequência de Toques (`TapSequenceRequest`)

Executa uma sequência ordenada de toques em diferentes coordenadas normalizadas.

- **Método:** `POST`
- **Rota:** `/tap-sequence`

**Campos do Request JSON (`TapSequenceRequest`):**

| Campo | Tipo | Obrigatório | Padrão | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `points` | `list[TapPoint]` | Sim | — | `min_length=1` | Lista de objetos `{"nx": int, "ny": int}` com `0 <= nx, ny <= 1000`. |
| `delay_ms` | `int` | Não | `50` | `>= 0` | Intervalo em milissegundos entre cada ponto da sequência. |

**Exemplo de Request:**
```json
{
  "points": [
    {"nx": 100, "ny": 200},
    {"nx": 800, "ny": 900}
  ],
  "delay_ms": 50
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Tapped sequence of 2 points: [[100, 200], [800, 900]].",
  "error": null
}
```

---

### 3.5. `POST /long-press` — Toque Longo (`LongPressRequest`)

Pressiona e mantém o toque na coordenada `(nx, ny)` pelo tempo especificado em `duration_ms`.

- **Método:** `POST`
- **Rota:** `/long-press`

**Campos do Request JSON (`LongPressRequest`):**

| Campo | Tipo | Obrigatório | Padrão | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `nx` | `int` | Sim | — | `0 <= nx <= 1000` | Coordenada X normalizada. |
| `ny` | `int` | Sim | — | `0 <= ny <= 1000` | Coordenada Y normalizada. |
| `duration_ms` | `int` | Não | `1000` | `>= 1` | Duração do pressionamento em milissegundos. |

**Exemplo de Request:**
```json
{
  "nx": 300,
  "ny": 400,
  "duration_ms": 1200
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Long pressed at [300, 400] for 1200ms.",
  "error": null
}
```

---

### 3.6. `POST /swipe` — Deslize / Arraste (`SwipeRequest`)

Executa um gesto linear de arraste/deslize do ponto inicial `(start_nx, start_ny)` até o ponto final `(end_nx, end_ny)`.

- **Método:** `POST`
- **Rota:** `/swipe`

**Campos do Request JSON (`SwipeRequest`):**

| Campo | Tipo | Obrigatório | Padrão | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `start_nx` | `int` | Sim | — | `0 <= start_nx <= 1000` | Coordenada X inicial normalizada. |
| `start_ny` | `int` | Sim | — | `0 <= start_ny <= 1000` | Coordenada Y inicial normalizada. |
| `end_nx` | `int` | Sim | — | `0 <= end_nx <= 1000` | Coordenada X final normalizada. |
| `end_ny` | `int` | Sim | — | `0 <= end_ny <= 1000` | Coordenada Y final normalizada. |
| `duration_ms` | `int` | Não | `400` | `>= 1` | Duração total do movimento em milissegundos. |

**Exemplo de Request:**
```json
{
  "start_nx": 500,
  "start_ny": 800,
  "end_nx": 500,
  "end_ny": 200,
  "duration_ms": 350
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Swiped from [500, 800] to [500, 200] in 350ms.",
  "error": null
}
```

---

### 3.7. `POST /input-text` — Digitação de Texto (`InputTextRequest`)

Insere texto no campo atualmente em foco no dispositivo.

> **Observação sobre foco e limpeza prévia:** Quando o agente solicita `input_text(text, target=(nx, ny), clear_exist=...)` no `HttpTouchActuator`, o próprio cliente executa primeiro uma chamada a `POST /focus-and-clear` (se `clear_exist=True`) ou `POST /tap` (se `clear_exist=False`) antes de chamar `POST /input-text`. Portanto, a rota `/input-text` precisa apenas enviar os caracteres para o elemento já focado.

- **Método:** `POST`
- **Rota:** `/input-text`

**Campos do Request JSON (`InputTextRequest`):**

| Campo | Tipo | Obrigatório | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- |
| `text` | `string` | Sim | `min_length=1` | Texto a ser inserido no campo em foco. |

**Exemplo de Request:**
```json
{
  "text": "usuario@exemplo.com"
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Input text 'usuario@exemplo.com'.",
  "error": null
}
```

---

### 3.8. `POST /focus-and-clear` — Focar e Limpar Campo de Texto (`FocusAndClearRequest`)

Toca no campo localizado em `(nx, ny)` para focá-lo e apaga todo o conteúdo de texto existente nele.

- **Método:** `POST`
- **Rota:** `/focus-and-clear`

**Campos do Request JSON (`FocusAndClearRequest`):**

| Campo | Tipo | Obrigatório | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- |
| `nx` | `int` | Sim | `0 <= nx <= 1000` | Coordenada X normalizada do campo de texto. |
| `ny` | `int` | Sim | `0 <= ny <= 1000` | Coordenada Y normalizada do campo de texto. |

**Exemplo de Request:**
```json
{
  "nx": 400,
  "ny": 300
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Focused at [400, 300] and cleared text.",
  "error": null
}
```

---

### 3.9. `POST /erase-char` — Apagar Caractere(s) (`EraseCharRequest`)

Envia o comando de apagar caractere (`KEYCODE_DEL` / Backspace) uma ou mais vezes no campo em foco. Aceita corpo vazio `{}` (assumindo `times=1`).

- **Método:** `POST`
- **Rota:** `/erase-char`

**Campos do Request JSON (`EraseCharRequest`):**

| Campo | Tipo | Obrigatório | Padrão | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `times` | `int` | Não | `1` | `>= 1` | Número de vezes que a tecla Backspace deve ser acionada. |

**Exemplo de Request:**
```json
{
  "times": 1
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Erased 1 character(s).",
  "error": null
}
```

---

### 3.10. `POST /press-key` — Pressionar Tecla do Sistema (`PressKeyRequest`)

Aciona uma tecla física/sistema do dispositivo.

- **Método:** `POST`
- **Rota:** `/press-key`

**Campos do Request JSON (`PressKeyRequest`):**

| Campo | Tipo | Obrigatório | Restrições | Descrição |
| :--- | :--- | :--- | :--- | :--- |
| `key` | `string` | Sim | `min_length=1` | Nome da tecla (ex.: `"BACK"`, `"HOME"`, `"ENTER"`, `"APP_SWITCH"`, `"VOLUME_UP"`, `"VOLUME_DOWN"`, `"POWER"`, `"TAB"`, `"SPACE"`, `"ESCAPE"`, `"DELETE"`) ou código numérico de keyevent Android. |

**Exemplo de Request:**
```json
{
  "key": "BACK"
}
```

**Exemplo de Response (`200 OK`):**
```json
{
  "ok": true,
  "message": "Pressed key 'BACK'.",
  "error": null
}
```

---

## 4. Checklist para Desenvolver Outras APIs Baseadas Neste Contrato

Ao criar uma nova implementação da Touch API (por exemplo, para controlar outro sistema operacional, hardware embarcado ou dispositivo via protocolo proprietário):

1. **Implemente todas as rotas acima** (`GET /health`, `POST /device`, `POST /tap`, `POST /tap-sequence`, `POST /long-press`, `POST /swipe`, `POST /input-text`, `POST /focus-and-clear`, `POST /erase-char`, `POST /press-key`).
2. **Valide os limites de coordenadas (`0..1000`)**: retorne status `422` com `{"ok": false, "message": "...", "error": "..."}` caso receba valores fora do intervalo.
3. **Converta de `0..1000` para a unidade nativa do seu atuador**: utilize a resolução obtida/configurada para transformar `(nx, ny)` em pixels, milímetros ou passos de motor.
4. **Respeite o formato `TouchActionResponse`**: sempre retorne um objeto JSON contendo `"ok"` (`bool`), `"message"` (`str`) e `"error"` (`str | null`).
5. **Aponte o ARTEMIS para sua nova API**:
   ```bash
   export ARTEMIS_ACTUATOR_BACKEND=http
   export ARTEMIS_TOUCH_API_URL=http://seu-servidor:8001
   ```

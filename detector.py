"""detector.py - Detector de Múltiplos Ícones e Elementos Visuais Desacoplado do Artemis.

Permite localizar múltiplos ícones e componentes visuais na tela a partir de uma imagem
ou diretamente do smartphone conectado via ADB, retornando as coordenadas normalizadas
(0-1000) e em pixels reais de cada elemento encontrado, sem executar nenhuma ação física.
"""

import argparse
import asyncio
import io
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from langchain_core.messages import HumanMessage

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.llm.structured import parse_structured
from artemis.services.llm import get_llm
from artemis.utils.image_scaling import encode_image_for_llm

COLOR_PALETTE = [
    "#00FFCC",  # Ciano Neon
    "#FF007F",  # Magenta
    "#FFD700",  # Dourado
    "#00BFFF",  # Azul Celeste
    "#32CD32",  # Verde Lima
    "#FF8C00",  # Laranja
    "#9D00FF",  # Roxo Elétrico
    "#FF1493",  # Rosa Choque
]


@dataclass
class DetectedItem:
    """Representa um elemento/ícone individual localizado na tela."""
    label: str
    normalized_coords: tuple[int, int]  # (x, y) na escala 0-1000 (x=horizontal, y=vertical)
    pixel_coords: tuple[int, int]       # (pixel_x, pixel_y) na resolução nativa da tela
    index: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "normalized_coords": list(self.normalized_coords),
            "pixel_coords": list(self.pixel_coords),
        }


@dataclass
class DetectionResult:
    """Resultado consolidado da detecção de múltiplos ícones."""
    query: str | list[str]
    found: bool
    total_found: int
    items: list[DetectedItem]
    screen_size: tuple[int, int]
    annotated_image_path: str | None
    elapsed_time: float
    raw_response: Any = None

    @property
    def normalized_coords(self) -> tuple[int, int] | None:
        """Compatibilidade: retorna coordenadas do primeiro item detectado."""
        return self.items[0].normalized_coords if self.items else None

    @property
    def pixel_coords(self) -> tuple[int, int] | None:
        """Compatibilidade: retorna pixels do primeiro item detectado."""
        return self.items[0].pixel_coords if self.items else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "found": self.found,
            "total_found": self.total_found,
            "items": [item.to_dict() for item in self.items],
            "screen_size": list(self.screen_size),
            "annotated_image_path": self.annotated_image_path,
            "elapsed_time": round(self.elapsed_time, 2),
        }


class IconDetector:
    """Detector visual de ícones desacoplado da execução de ações."""

    def __init__(
        self,
        device_serial: str = "N0OJ460182",
        role_name: str = "operator",
        timeout: float = 60.0,
    ):
        self.device_serial = device_serial
        self.role_name = role_name
        self.timeout = timeout
        self.device = DeviceContext(platform=DevicePlatform.ANDROID, device_id=device_serial)
        self.ctx = ArtemisContext(device=self.device)
        self._llm = None

    @property
    def llm(self):
        if self._llm is None:
            try:
                self._llm = get_llm(self.ctx, name="object_detector", is_utils=True)
            except Exception:
                self._llm = get_llm(self.ctx, name=self.role_name)
        return self._llm

    def capture_screen(self, device_serial: str | None = None) -> Image.Image:
        """Captura um screenshot direto do dispositivo via ADB contornando banners."""
        serial = device_serial or self.device_serial
        cmd = ["adb", "-s", serial, "exec-out", "screencap", "-p"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        # Ignora avisos/banners procurando pelo header PNG padrão (\x89PNG\r\n\x1a\n)
        png_idx = res.stdout.find(b"\x89PNG\r\n\x1a\n")
        if png_idx == -1:
            raise RuntimeError(f"Erro ao capturar screenshot via adb: {res.stderr.decode(errors='replace')}")
        return Image.open(io.BytesIO(res.stdout[png_idx:]))

    async def detect(
        self,
        image: Image.Image | str | Path,
        query: str | list[str],
        output_path: str | Path | None = "detected_result.png",
        max_dim: int = 1024,
    ) -> DetectionResult:
        """Localiza múltiplos ícones ou elementos visuais na imagem fornecida.

        Args:
            image: Instância de PIL.Image ou caminho para o arquivo de imagem.
            query: Descrição ou lista de ícones a localizar (ex: 'flash on, flash off, flash auto').
            output_path: Caminho para salvar a imagem anotada com as miras.
            max_dim: Dimensão máxima para downscaling enviado ao LLM (otimiza inferência).

        Returns:
            DetectionResult com todos os elementos encontrados e metadados.
        """
        start_time = time.time()

        # 1. Normaliza a query
        if isinstance(query, (list, tuple)):
            labels_str = ", ".join(str(q).strip() for q in query if str(q).strip())
            query_repr = list(query)
        else:
            labels_str = str(query).strip()
            query_repr = str(query).strip()

        # 2. Carrega a imagem original
        if isinstance(image, (str, Path)):
            pil_img = Image.open(image)
        else:
            pil_img = image

        orig_w, orig_h = pil_img.size

        # 3. Converte imagem para bytes para downscaling inteligente
        img_buffer = io.BytesIO()
        pil_img.save(img_buffer, format="PNG")
        raw_bytes = img_buffer.getvalue()

        # 4. Prepara a imagem otimizada para o LLM
        img_b64 = encode_image_for_llm(raw_bytes, max_dim=max_dim)

        # 5. Prompt de detecção posicional com coordenadas [x, y] explícitas
        prompt = (
            f"Detect the locations of all requested UI components/icons on the screen: {labels_str}.\n"
            "Return a JSON list containing all detected objects. Each object must have:\n"
            "- 'point': [x, y] normalized coordinates (0 to 1000), where x is horizontal distance from left (0=left, 1000=right), and y is vertical distance from top (0=top, 1000=bottom)\n"
            "- 'label': the specific name or category of the detected object\n\n"
            "Example output:\n"
            "[\n"
            '  {"point": [840, 900], "label": "camera icon"},\n'
            '  {"point": [160, 900], "label": "phone icon"}\n'
            "]\n\n"
            "If no matching objects are found, return an empty list []."
        )

        messages = [
            HumanMessage(
                content=[
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ]
            )
        ]

        # 6. Executa a inferência
        response = await asyncio.wait_for(self.llm.ainvoke(messages), timeout=self.timeout)
        content = response.content if isinstance(response.content, str) else str(response.content)

        parsed = parse_structured(content)
        elapsed = time.time() - start_time

        if not parsed or not isinstance(parsed, list):
            return DetectionResult(
                query=query_repr,
                found=False,
                total_found=0,
                items=[],
                screen_size=(orig_w, orig_h),
                annotated_image_path=None,
                elapsed_time=elapsed,
                raw_response=content,
            )

        # 7. Processa todos os itens detectados
        detected_items: list[DetectedItem] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict) or "point" not in item:
                continue
            point = item["point"]
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue

            x_norm, y_norm = int(point[0]), int(point[1])
            # Converte para pixels reais na resolução nativa da tela
            pixel_x = int(round(x_norm * orig_w / 1000.0))
            pixel_y = int(round(y_norm * orig_h / 1000.0))
            label = str(item.get("label") or f"Elemento {idx}")

            detected_items.append(
                DetectedItem(
                    label=label,
                    normalized_coords=(x_norm, y_norm),
                    pixel_coords=(pixel_x, pixel_y),
                    index=idx,
                )
            )

        # 8. Anotação visual com todas as miras na imagem
        saved_img_path = None
        if output_path and detected_items:
            saved_img_path = str(output_path)
            self._draw_all_markers(pil_img, detected_items, saved_img_path)

        return DetectionResult(
            query=query_repr,
            found=len(detected_items) > 0,
            total_found=len(detected_items),
            items=detected_items,
            screen_size=(orig_w, orig_h),
            annotated_image_path=saved_img_path,
            elapsed_time=elapsed,
            raw_response=parsed,
        )

    @staticmethod
    def _draw_all_markers(
        image: Image.Image,
        items: list[DetectedItem],
        output_path: str,
    ) -> None:
        """Desenha mira, emblema numérico e rótulo de todos os itens detectados."""
        annotated = image.copy()
        draw = ImageDraw.Draw(annotated)
        w, h = annotated.size

        radius = max(22, int(w * 0.024))

        for idx, item in enumerate(items):
            color = COLOR_PALETTE[idx % len(COLOR_PALETTE)]
            px, py = item.pixel_coords
            x_norm, y_norm = item.normalized_coords

            # Círculo externo
            draw.ellipse([px - radius, py - radius, px + radius, py + radius], outline=color, width=4)
            # Mira central
            draw.line([px - radius - 12, py, px + radius + 12, py], fill=color, width=2)
            draw.line([px, py - radius - 12, px, py + radius + 12], fill=color, width=2)
            # Ponto central
            draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill="#FF0033")

            # Emblema numérico [1], [2] acima do círculo
            badge_text = f"[{item.index}]"
            draw.rectangle([px - 14, py - radius - 24, px + 14, py - radius - 2], fill=color)
            draw.text((px - 9, py - radius - 20), badge_text, fill="black")

            # Rótulo de texto com coordenadas abaixo
            text = f"{item.index}. {item.label} ({px}, {py})"
            box_w = min(len(text) * 10 + 16, w - px + radius)
            box_y1 = min(py + radius + 6, h - 35)
            box_y2 = min(box_y1 + 28, h - 2)
            draw.rectangle([px - radius, box_y1, px - radius + box_w, box_y2], fill="black", outline=color, width=1)
            draw.text((px - radius + 8, box_y1 + 6), text, fill="white")

        annotated.save(output_path)


# ==============================================================================
# Funções de Conveniência (API Simples)
# ==============================================================================

def detect_icon(
    image_path: str | Path,
    query: str | list[str],
    output_path: str | Path | None = "detected_result.png",
    device_serial: str = "N0OJ460182",
) -> DetectionResult:
    """Localiza um ou múltiplos ícones em uma imagem existente (sem ações físicas)."""
    detector = IconDetector(device_serial=device_serial)
    return asyncio.run(detector.detect(image=image_path, query=query, output_path=output_path))


def capture_and_detect(
    query: str | list[str],
    device_serial: str = "N0OJ460182",
    output_path: str | Path | None = "detected_result.png",
) -> DetectionResult:
    """Captura um screenshot do dispositivo e localiza os ícones solicitados."""
    detector = IconDetector(device_serial=device_serial)
    img = detector.capture_screen(device_serial)
    return asyncio.run(detector.detect(image=img, query=query, output_path=output_path))


# ==============================================================================
# Execução via Linha de Comando (CLI)
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Detector de Múltiplos Ícones e Elementos Visuais Desacoplado do Artemis"
    )
    parser.add_argument(
        "queries",
        nargs="*",
        default=["all flash options", "all zoom options"],
        help="Nomes ou descrições dos ícones/elementos a localizar (ex: 'flash on' 'flash off' ou 'camera, phone, settings')",
    )
    parser.add_argument("--query", "-q", action="append", help="Adiciona uma query individual de busca (pode repetir: -q 'item1' -q 'item2')")
    parser.add_argument("--image", "-i", default=None, help="Caminho para imagem local (se omitido, captura da tela)")
    parser.add_argument("--device", "-d", default="N0OJ460182", help="Serial ADB do dispositivo (padrão: N0OJ460182)")
    parser.add_argument("--output", "-o", default="detected_result.png", help="Arquivo onde salvar a imagem anotada")
    parser.add_argument("--json", action="store_true", help="Imprime o resultado em formato JSON")

    args = parser.parse_args()

    # Consolida as queries
    all_queries = []
    if args.query:
        all_queries.extend(args.query)
    if args.queries:
        for q in args.queries:
            sub_queries = [s.strip() for s in re.split(r"[,;|]", q) if s.strip()]
            all_queries.extend(sub_queries)

    final_queries = list(dict.fromkeys(all_queries)) or ["flash on", "flash off", "flash auto"]

    detector = IconDetector(device_serial=args.device)

    print(f"🔍 Buscando {len(final_queries)} elementos: {final_queries}...")
    if args.image:
        print(f"🖼️  Usando imagem local: {args.image}")
        res = asyncio.run(detector.detect(image=args.image, query=final_queries, output_path=args.output))
    else:
        print(f"📱 Capturando tela do dispositivo: {args.device}...")
        img = detector.capture_screen(args.device)
        res = asyncio.run(detector.detect(image=img, query=final_queries, output_path=args.output))

    if args.json:
        print(json.dumps(res.to_dict(), indent=2, ensure_ascii=False))
        return

    print("\n" + "=" * 60)
    if res.found:
        print(f"✅ {res.total_found} ícone(s) ENCONTRADO(S) em {res.elapsed_time:.2f}s!\n")
        for item in res.items:
            print(f"  [{item.index}] {item.label}:")
            print(f"      📍 Normalizado (x, y): {item.normalized_coords}")
            print(f"      🎯 Pixels na tela ({res.screen_size[0]}x{res.screen_size[1]}): {item.pixel_coords}\n")
        if res.annotated_image_path:
            print(f"🖼️  Imagem anotada com todas as miras salva em: {res.annotated_image_path}")
    else:
        print(f"❌ Nenhum elemento foi localizado para {final_queries} em {res.elapsed_time:.2f}s.")
    print("=" * 60)


if __name__ == "__main__":
    main()

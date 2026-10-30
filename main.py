import subprocess
import time
from contextlib import contextmanager

from artemis import ArtemisRunner
from artemis.toolchain import find_scrcpy


def mirror_screen(
    device_serial: str,
    title: str = "Artemis - Smartphone Mirror",
    always_on_top: bool = True,
) -> subprocess.Popen | None:
    """Inicia o espelhamento da tela via scrcpy em segundo plano."""
    scrcpy_bin = find_scrcpy() or "scrcpy"
    try:
        cmd = [
            scrcpy_bin,
            "--serial",
            device_serial,
            "--window-title",
            title,
            "--stay-awake",
        ]
        if always_on_top:
            cmd.append("--always-on-top")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.0)  # Aguarda a janela do scrcpy carregar
        return proc
    except Exception as e:
        print(f"[Aviso] Não foi possível iniciar o espelhamento via scrcpy: {e}")
        return None


@contextmanager
def start_screen_mirror(
    device_serial: str,
    title: str = "Artemis - Smartphone Mirror",
    auto_close: bool = True,
    always_on_top: bool = True,
):
    """Context manager que mantém a tela espelhada durante a execução."""
    proc = mirror_screen(device_serial, title=title, always_on_top=always_on_top)
    try:
        yield proc
    finally:
        if auto_close and proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


DEVICE_SERIAL = "N0OJ460182"

# Espelha a tela em segundo plano enquanto executa o bot
# Dica: defina auto_close=False se preferir manter a janela do scrcpy aberta após a automação
with start_screen_mirror(device_serial=DEVICE_SERIAL, auto_close=True):
    with ArtemisRunner(device_serial=DEVICE_SERIAL, profile="flash") as bot:
        # Basic task execution
        start_time = time.time()
        result = bot.run(
            "Open the camera app, turn the flash on, then turn it off, and finally select the automatic option.",
            return_steps=True,
        )
        end_time = time.time()
        print(f"Execution time: {end_time - start_time:.2f} seconds")

if result and "steps" in result:
    for step in result["steps"]:
        print(f"\nPasso {step['step_number']}: {step['command']}")
        print(f"  Coordenadas (0-1000): {step['coordinates']}")
        print(f"  Pixels reais na tela: {step['pixel_coordinates']}")
        print(f"  Argumentos: {step['args']}")
        print(f"  Status da ação: {step['status']}")

"""
visual.py — Template matching via screenshot do Playwright + OpenCV.
"""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import cv2
import numpy as np
from playwright.async_api import Page

RESOURCES = Path(__file__).parent / "resources"

SCALES = [1.0, 0.85, 0.70]  # era 7 escalas — 3 é suficiente e ~3x mais rápido

_template_cache: dict[str, list[tuple[np.ndarray, float]]] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cv2")


def _load_scaled_templates(template_path: Path) -> list[tuple[np.ndarray, float]]:
    """Carrega o template em grayscale e pré-calcula todas as escalas (cache por arquivo)."""
    key = str(template_path)
    if key in _template_cache:
        return _template_cache[key]

    tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        return []

    scaled = []
    for scale in SCALES:
        h = max(1, int(tpl.shape[0] * scale))
        w = max(1, int(tpl.shape[1] * scale))
        scaled.append((cv2.resize(tpl, (w, h)), scale))

    _template_cache[key] = scaled
    return scaled


def _match_multiscale(
    screen_gray: np.ndarray,
    scaled_templates: list[tuple[np.ndarray, float]],
    threshold: float,
) -> tuple[float, int, int]:
    s_h, s_w = screen_gray.shape[:2]
    best_val, best_cx, best_cy = 0.0, 0, 0

    for resized, _ in scaled_templates:
        t_h, t_w = resized.shape[:2]
        if t_h > s_h or t_w > s_w:
            continue
        result = cv2.matchTemplate(screen_gray, resized, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val > best_val:
            best_val = max_val
            best_cx = max_loc[0] + t_w // 2
            best_cy = max_loc[1] + t_h // 2

    return best_val, best_cx, best_cy


async def find_and_click(
    page: Page,
    template_name: str,
    matching: float = 0.97,
    waiting_time: float = 10,
    offset_x: int = 0,
    offset_y: int = 0,
) -> bool:
    template_path = RESOURCES / f"{template_name}.png"
    if not template_path.exists():
        print(f"    ⚠ Template não encontrado: {template_path}")
        return False

    scaled_templates = _load_scaled_templates(template_path)
    if not scaled_templates:
        print(f"    ⚠ Falha ao carregar imagem: {template_path}")
        return False

    loop = asyncio.get_event_loop()
    deadline = time.monotonic() + waiting_time
    best_val = 0.0

    while time.monotonic() < deadline:
        screenshot_bytes = await page.screenshot()
        screen_gray = cv2.imdecode(
            np.frombuffer(screenshot_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE
        )

        val, cx, cy = await loop.run_in_executor(
            _executor, _match_multiscale, screen_gray, scaled_templates, matching
        )
        if val > best_val:
            best_val = val

        if val >= matching:
            await page.mouse.click(cx + offset_x, cy + offset_y)
            print(f"    ✓ '{template_name}' encontrado (conf={val:.2f}) → clicou em ({cx + offset_x}, {cy + offset_y})")
            return True

        await asyncio.sleep(1.0)

    print(f"    ✗ '{template_name}' não encontrado após {waiting_time}s (melhor conf={best_val:.2f})")
    return False


async def find(
    page: Page,
    template_name: str,
    matching: float = 0.97,
    waiting_time: float = 10,
) -> bool:
    """Verifica se o template está visível (multi-scale), sem clicar."""
    template_path = RESOURCES / f"{template_name}.png"
    if not template_path.exists():
        return False

    scaled_templates = _load_scaled_templates(template_path)
    if not scaled_templates:
        return False

    loop = asyncio.get_event_loop()
    deadline = time.monotonic() + waiting_time

    while time.monotonic() < deadline:
        screenshot_bytes = await page.screenshot()
        screen_gray = cv2.imdecode(
            np.frombuffer(screenshot_bytes, dtype=np.uint8), cv2.IMREAD_GRAYSCALE
        )

        val, _, _ = await loop.run_in_executor(
            _executor, _match_multiscale, screen_gray, scaled_templates, matching
        )
        if val >= matching:
            return True

        await asyncio.sleep(1.0)

    return False

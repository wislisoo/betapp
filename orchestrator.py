"""
orchestrator.py — Vermelho0 Fortune Gems Automation
"""

import asyncio
import datetime
import logging
import subprocess
import sys
from pathlib import Path

import httpx
import requests as _http
from playwright.async_api import async_playwright, BrowserContext, Page, Playwright

import config
from account_manager import (Account, load_accounts_from_txt, load_accounts_by_range,
                             pop_next_account, save_account_success, release_account)
from visual import find_and_click
from backend_redeemer import broadcast_via_api, warm_up, close_session, http_keepalive
from code_tester import run_testers
from game_cdp import take_over_tab, GameTab

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("timbet")

# Suprime logs internos de bibliotecas
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("hpack").setLevel(logging.WARNING)

BYPASS_JS = (Path(__file__).parent / "inactivity_bypass.js").read_text(encoding="utf-8")


SCREENSHOTS_DIR = Path(__file__).parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# --- Proxies (formato: host:port:usuario:senha) ---
def _load_proxies() -> list[str]:
    p = Path(__file__).parent / "proxies.txt"
    if not p.exists():
        return []
    lines = [l.strip() for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    result = []
    for line in lines:
        parts = line.split(":")
        if len(parts) >= 2:
            host, port = parts[0], parts[1]
            result.append(f"http://{host}:{port}")
    return result

_PROXIES = _load_proxies()

def _proxy_for(index: int) -> str | None:
    if not config.USE_PROXY or not _PROXIES:
        return None
    return _PROXIES[(index - 1) % len(_PROXIES)]

# --- Caminho do Chrome real (sem flags de automação do Playwright) ---
_CHROME_PATHS = [
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/snap/bin/chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_EXE = next((p for p in _CHROME_PATHS if Path(p).exists()), None)

STEALTH_JS = """
(function() {
    if (!window.chrome) {
        try {
            Object.defineProperty(window, 'chrome', {
                value: { runtime: {}, loadTimes: function(){}, csi: function(){} },
                configurable: true,
                writable: true
            });
        } catch(e) {}
    }
    try {
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    } catch(e) {}
})();
"""

_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_start_time: dict[int, float] = {}  # monotonic timestamp de quando o Chrome foi lançado

active_tabs:          dict[int, GameTab]   = {}
active_accounts:      dict[int, Account]  = {}
account_tokens:       dict[int, str]      = {}
account_launch_hdrs:  dict[int, dict]     = {}   # kid por conta (para rotação via API)

# Controle de cpulimit: renderer_pid → processo cpulimit; ports pausados durante Playwright
_limited_renderers:   dict[int, subprocess.Popen] = {}
_cpulimit_paused_ports: set[int] = set()
promo_lock            = asyncio.Lock()
_reenter_sem          = asyncio.Semaphore(3)   # max 3 reenter_game simultâneos
_restarting_accounts: set[int] = set()         # accounts com Chrome sendo reiniciado por memória
_playwright: "Playwright | None" = None

_LAUNCH_URL  = "https://bet.app/api/thirdGame/launchUrl"   # PENDENTE: confirmar endpoint
_LAUNCH_BODY = {
    "gameId": 551476, "demo": 0,
    "host": "https://bet.app",
    "backUrl": "https://bet.app/pg/callback.html",          # PENDENTE
    "launchMode": "html", "mobile": 0,
}


# ---------------------------------------------------------------------------
# SCREENSHOT
# ---------------------------------------------------------------------------

async def take_screenshot(page: Page, index: int, label: str) -> None:
    ts = datetime.datetime.now().strftime("%H%M%S")
    path = SCREENSHOTS_DIR / f"acc{index:02d}_{label}_{ts}.png"
    await page.screenshot(path=str(path), full_page=False)
    log.info(f"[{index:02d}] Screenshot salvo: {path.name}")


# ---------------------------------------------------------------------------
# VERIFICAÇÃO DE LOGIN
# ---------------------------------------------------------------------------

async def _logado_check(page: Page, timeout: float = 20.0) -> bool:
    """Retorna True se detectar sessão ativa: link de depósito OU popup de CPF (primeiro login)."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await page.locator('a[href="/deposit"]').count() > 0:
                return True
            if await page.locator('.van-popup--center').filter(has_text="Vinculação").count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# LOGIN GOOGLE OAUTH
# ---------------------------------------------------------------------------

async def _google_login(page: Page, account: Account, index: int, retries: int = 3) -> bool:
    """Clica no botão Google do casino, trata popup OAuth e confirma login."""
    for attempt in range(1, retries + 1):
        try:
            await page.goto(config.LOGIN_URL, timeout=config.LOGIN_TIMEOUT * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            # Clica no botão Google e aguarda popup abrir
            async with page.context.expect_page(timeout=15_000) as popup_info:
                await page.locator(config.GOOGLE_BTN_SEL).first.click()
            popup = await popup_info.value
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass

            try:
                await popup.add_init_script(STEALTH_JS)
            except Exception:
                pass
            log.info(f"[{index:02d}] Popup Google aberto (tentativa {attempt})")

            # Aguarda dinamicamente o que aparecer: chooser com a conta OU campo de email
            _chooser_sel = f'[data-identifier="{account.login}"], [data-email="{account.login}"]'
            _email_sel   = 'input[type="email"]'
            try:
                await popup.wait_for_selector(
                    f'{_chooser_sel}, {_email_sel}', timeout=12_000
                )
            except Exception:
                pass

            if await popup.locator(_chooser_sel).count() > 0:
                await popup.locator(_chooser_sel).first.click()
                log.info(f"[{index:02d}] Conta selecionada no chooser")
                await asyncio.sleep(1.0)
            else:
                log.info(f"[{index:02d}] Formulário de email detectado")

                # "Use another account" se houver outras contas listadas antes do form
                for use_other in [
                    '#use_another_account',
                    'li:has-text("Use another account")',
                    'li:has-text("Usar outra conta")',
                    'div.riDSKb:has-text("Use another account")',
                ]:
                    try:
                        btn = popup.locator(use_other)
                        if await btn.count() > 0:
                            await btn.first.click()
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass

                # Formulário: e-mail → Next → senha → Next
                await popup.locator('input[type="email"]').fill(account.login, timeout=10_000)
                await popup.locator(
                    '#identifierNext, button:has-text("Next"), button:has-text("Próximo")'
                ).first.click(timeout=5_000)

                await popup.wait_for_selector('input[type="password"]:not([aria-hidden="true"])', timeout=15_000)
                await popup.locator('input[type="password"]:not([aria-hidden="true"])').fill(account.senha)
                await popup.locator(
                    '#passwordNext, button:has-text("Next"), button:has-text("Próximo")'
                ).first.click(timeout=5_000)

            # Telas de consentimento — loop de 6 tentativas
            # Cobre: "I understand" (primeira vez) → "Continue" (pós-chooser e pós-consent)
            for _ in range(6):
                if popup.is_closed():
                    break
                for entendi in [
                    'button:has-text("I understand")',
                    'button:has-text("Entendi")',
                    'button:has-text("Concordo")',
                ]:
                    try:
                        btn = popup.locator(entendi)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=3_000)
                            await asyncio.sleep(0.8)
                    except Exception:
                        pass
                clicou = False
                for continuar in [
                    'button:has-text("Continue")',
                    'button:has-text("Continuar")',
                    'button:has-text("Permitir")',
                    'span.VfPpkd-vQzf8d:has-text("Continue")',
                ]:
                    try:
                        btn = popup.locator(continuar)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=4_000)
                            clicou = True
                            break
                    except Exception:
                        pass
                if clicou:
                    await asyncio.sleep(1.0)
                    if popup.is_closed():
                        break
                else:
                    await asyncio.sleep(1.5)

            # Aguarda popup fechar (max 30s)
            if not popup.is_closed():
                try:
                    await popup.wait_for_event("close", timeout=30_000)
                except Exception:
                    pass

            if await _logado_check(page, timeout=15.0):
                log.info(f"[{index:02d}] Google OAuth OK (tentativa {attempt})")
                return True

            await page.screenshot(path=f"/home/betapp/debug_main_{attempt}.png")
            if not popup.is_closed():
                await popup.screenshot(path=f"/home/betapp/debug_popup_{attempt}.png")
            log.warning(f"[{index:02d}] Google login tentativa {attempt}/{retries}: sessão não confirmada")

        except Exception as exc:
            try:
                await page.screenshot(path=f"/home/betapp/debug_main_{attempt}.png")
                if 'popup' in dir() and not popup.is_closed():
                    await popup.screenshot(path=f"/home/betapp/debug_popup_{attempt}.png")
            except Exception:
                pass
            log.warning(f"[{index:02d}] Google login tentativa {attempt}/{retries} falhou: {exc}")
            await asyncio.sleep(2.0)

    log.error(f"[{index:02d}] Google login falhou após {retries} tentativas")
    return False


# ---------------------------------------------------------------------------
# VERIFICAÇÃO VISUAL PÓS-BROADCAST — entra código na UI
# ---------------------------------------------------------------------------

async def _visual_verify(code: str) -> None:
    """Usa o pychrome já conectado para digitar e confirmar o código de forma stealth."""
    if not active_tabs:
        return
        
    log.info(f"Injetando código '{code}' visualmente no navegador (modo stealth)...")
    import config
    import time
    
    try:
        for idx, tab in active_tabs.items():
            loop = asyncio.get_event_loop()
            
            # Tenta clicar em várias alturas acima do botão Confirmar para garantir que o EditBox seja focado
            cx, cy = config.CALIB_CONFIRMAR
            for offset in [60, 80, 100, 120]:
                await loop.run_in_executor(None, tab.click, cx, cy - offset)
                await asyncio.sleep(0.1)

            # Insere o texto usando insertText diretamente (mais confiável que char-por-char)
            def _insert_code():
                tab._tab.Input.insertText(text=code)
                
            await loop.run_in_executor(None, _insert_code)
            await asyncio.sleep(0.5)

            # Clica confirmar
            await loop.run_in_executor(None, tab.click, cx, cy)
            log.info(f"[{idx:02d}] Código submetido no navegador!")
            
    except Exception as exc:
        log.error(f"Falha na injeção visual: {exc}")


# ---------------------------------------------------------------------------
# BROADCAST
# ---------------------------------------------------------------------------

async def broadcast_promo(code: str) -> None:
    log.info(f"BROADCAST '{code}' — {len(active_tabs)} conta(s)")
    if account_tokens:
        await broadcast_via_api(account_tokens, code)
    else:
        log.warning("Sem tokens — fallback visual não disponível")
        
    asyncio.create_task(_visual_verify(code))


# ---------------------------------------------------------------------------
# SETUP DE UMA CONTA
# ---------------------------------------------------------------------------

async def _spawn_chrome(account: Account) -> tuple[int, subprocess.Popen]:
    """Lança Chrome real via subprocess — sem fingerprint de automação do Playwright."""
    if not CHROME_EXE:
        raise RuntimeError("Chrome não encontrado. Verifique o caminho em orchestrator.py.")

    port = 9300 + account.index
    account.profile_dir.mkdir(parents=True, exist_ok=True)

    # Mata processo anterior na mesma porta (reinício limpo) — incluindo filhos
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] and "chrome" in proc.info["name"].lower():
                    if any(f"--remote-debugging-port={port}" in arg for arg in (proc.info["cmdline"] or [])):
                        p = psutil.Process(proc.info["pid"])
                        children = p.children(recursive=True)
                        for child in children:
                            try:
                                child.kill()
                            except Exception:
                                pass
                        p.kill()
                        psutil.wait_procs(children + [p], timeout=5)
            except Exception:
                pass
    except ImportError:
        pass

    # Remove lock files do perfil que bloqueiam nova instância
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        p = account.profile_dir / lock
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    args = [
        CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={account.profile_dir}",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-popup-blocking",
        "--mute-audio",
        "--disable-default-apps",
        "--no-first-run",
        "--force-device-scale-factor=1",
        "--disable-pinch",
        "--window-size=320,640",
        "--window-position=0,0",
        "--disable-gpu-vsync",
        "--disable-sync",
        "--disable-background-mode",
        "--disable-extensions",
        "--ignore-gpu-blocklist",
        "--disable-gpu-sandbox",
        "--use-gl=angle",
        "--use-angle=swiftshader",
        "--enable-webgl",
        "--enable-webgl2",
        "--disable-threaded-animation",
        "--disable-accelerated-2d-canvas",
        "--disable-canvas-aa",
        "--disable-background-networking",
        "--disable-domain-reliability",
        "--disable-component-update",
        "--disable-features=TranslateUI,MediaRouter,BackForwardCache,IsolateOrigins,site-per-process",
        "--disable-site-isolation-trials",
        "--renderer-process-limit=2",
        "--enable-low-end-device-mode",
        "--disk-cache-size=1",
        "--media-cache-size=1",
        "--disable-gpu-shader-disk-cache",
        "--max-decoded-image-size-mb=64",
        "--js-flags=--max-old-space-size=512",
    ]
    if config.HEADLESS:
        args += [
            "--headless=new",
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        ]

    proxy = _proxy_for(account.index)
    if proxy:
        import base64
        pac_path = Path(__file__).parent / "proxy.pac"
        pac_b64 = base64.b64encode(pac_path.read_bytes()).decode()
        pac_data_uri = f"data:application/x-javascript-config;base64,{pac_b64}"
        args += [f"--proxy-pac-url={pac_data_uri}"]
        log.info(f"[{account.index:02d}] PAC proxy ativo (data URI)")
    else:
        args += ["--no-proxy-server"]

    proc = subprocess.Popen(args)
    _chrome_procs[account.index] = proc
    _chrome_start_time[account.index] = asyncio.get_event_loop().time()

    # Aguarda Chrome estar pronto
    for _ in range(40):
        try:
            r = _http.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            if r.status_code == 200:
                log.info(f"[{account.index:02d}] Chrome pronto na porta {port}")
                return port, proc
        except Exception:
            pass
        await asyncio.sleep(0.5)

    raise RuntimeError(f"[{account.index:02d}] Chrome não respondeu na porta {port}")


def _run_game_flow(game_tab: "GameTab", index: int) -> bool:
    """Síncrono — roda em executor. Aguarda jogo carregar e executa fluxo visual."""
    log.info(f"[{index:02d}] [pychrome] Aguardando jogo renderizar ou token (max 120s)...")
    loaded = game_tab.wait_for_game_load(timeout=120)
    if not loaded:
        if game_tab.token:
            log.info(f"[{index:02d}] [pychrome] Canvas não renderizou mas token capturado — continuando")
        else:
            log.warning(f"[{index:02d}] [pychrome] Canvas não renderizou e sem token em 120s")
            return False
    else:
        log.info(f"[{index:02d}] [pychrome] Jogo carregado! Iniciando fluxo visual...")
    return game_tab.run_visual_flow()


async def setup_account(playwright: Playwright, account: Account, stagger: int = 0) -> None:
    if stagger > 0:
        await asyncio.sleep(stagger * config.HEADLESS_STAGGER)

    port, _proc = await _spawn_chrome(account)

    browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
    page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    cdp = await page.context.new_cdp_session(page)
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    await cdp.detach()
    log.info(f"[{account.index:02d}] STEALTH injetado")

    # Captura cookie+kid do launchUrl antes do pychrome assumir
    _pw_launch_headers: dict = {}
    def _on_pw_request(request):
        if "thirdGame/launchUrl" in request.url and not _pw_launch_headers:
            h = request.headers
            cookie = h.get("cookie")
            kid    = h.get("kid")
            if cookie:
                _pw_launch_headers["cookie"] = cookie
                if kid:
                    _pw_launch_headers["kid"] = kid
                log.info(f"[{account.index:02d}] launch_headers capturados")
    page.on("request", _on_pw_request)

    # Intercepta resposta do launchUrl para obter game_url
    captured: dict = {}
    async def _intercept_launch(route):
        response = await route.fetch()
        try:
            body = await response.json()
            data = body.get("data", "")
            if isinstance(data, str) and data.startswith("http"):
                captured["game_url"] = data
        except Exception:
            pass
        await route.fulfill(response=response)
    await page.route("**/thirdGame/launchUrl", _intercept_launch)

    # Verifica se já está logado navegando para GameList (sessão ativa = não redireciona para login)
    try:
        await page.goto(config.GAMELIST_URL, timeout=config.NAV_TIMEOUT * 1000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10_000)
        except Exception:
            pass

        already_logged = "/login" not in page.url and await _logado_check(page, timeout=5.0)
        if not already_logged:
            log.info(f"[{account.index:02d}] Iniciando Google OAuth: {account.login}")
            ok = await _google_login(page, account, account.index)
            if not ok:
                log.error(f"[{account.index:02d}] Google OAuth falhou — pulando conta")
                release_account(account)
                await browser.close()
                return
        else:
            log.info(f"[{account.index:02d}] Sessão ativa detectada — prosseguindo")
    except Exception as exc:
        log.error(f"[{account.index:02d}] Falha na autenticação: {exc}")
        release_account(account)
        await browser.close()
        return

    # Navega para GameList, encontra o jogo e clica JOGUE
    try:
        await page.goto(config.GAMELIST_URL, timeout=config.NAV_TIMEOUT * 1000)
        await page.wait_for_load_state("networkidle")

        try:
            close_btn = page.locator(".closeImg")
            await close_btn.wait_for(state="visible", timeout=4_000)
            await close_btn.click()
        except Exception:
            pass

        game_card = page.locator(f'.bgImg:has(img[src*="{config.GAME_IMAGE_URL_FRAGMENT}"])').first
        await game_card.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT * 1000)
        await game_card.dispatch_event("click")

        play_btn = page.locator('span.mode:has-text("JOGUE"), button:has-text("JOGUE")').first
        await play_btn.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT * 1000)
        await play_btn.dispatch_event("click")
        log.info(f"[{account.index:02d}] JOGUE clicado")
    except Exception as exc:
        log.error(f"[{account.index:02d}] Falha ao navegar para GameList: {exc}")
        await browser.close()
        return

    # Aguarda launchUrl ser interceptado
    for _ in range(60):
        if "game_url" in captured:
            break
        await asyncio.sleep(0.5)

    # Valida URL do jogo — deve ser do servidor WB, não do casino
    _casino_host = config.LOGIN_URL.split("/")[2]
    game_url = captured.get("game_url", "")
    if not game_url or _casino_host in game_url:
        log.warning(
            f"[{account.index:02d}] URL do jogo inválida ('{game_url or 'vazia'}') — "
            f"abra o jogo manualmente no navegador. Aguardando até {config.GAME_URL_WAIT}s..."
        )
        for _ in range(config.GAME_URL_WAIT * 2):
            await asyncio.sleep(0.5)
            gurl = captured.get("game_url", "")
            if gurl and _casino_host not in gurl:
                log.info(f"[{account.index:02d}] URL do jogo corrigida — continuando")
                break
        else:
            log.error(f"[{account.index:02d}] Jogo não abriu em {config.GAME_URL_WAIT}s — pulando conta")
            await browser.close()
            return

    # Encontra tab_id da aba atual
    import requests as req
    resp = req.get(f"http://127.0.0.1:{port}/json")
    tabs = resp.json()
    target_id = next((t["id"] for t in tabs if page.url.split("#")[0] in t["url"]), tabs[0]["id"])

    kid = captured.get("kid", "") or _pw_launch_headers.get("kid", "")
    log.info(f"[{account.index:02d}] Tab ID: {target_id} | kid={'present' if kid else 'absent'} | game_url={'present' if captured.get('game_url') else 'absent'}")

    # Salva headers para rotação
    if _pw_launch_headers:
        account_launch_hdrs[account.index] = _pw_launch_headers
        log.info(f"[{account.index:02d}] launch_headers salvos: cookie={'present' if _pw_launch_headers.get('cookie') else 'absent'}, kid={'present' if _pw_launch_headers.get('kid') else 'absent'}")
    elif kid:
        account_launch_hdrs[account.index] = {"kid": kid}

    await browser.close()
    log.info(f"[{account.index:02d}] Playwright desconectado")

    loop = asyncio.get_event_loop()
    game_tab: "GameTab | None" = await loop.run_in_executor(
        None, take_over_tab, port, target_id, BYPASS_JS
    )
    if game_tab is None:
        log.error(f"[{account.index:02d}] Falha ao assumir aba via pychrome")
        return

    visual_ok = await loop.run_in_executor(None, _run_game_flow, game_tab, account.index)
    if not visual_ok:
        game_tab.stop()
        return

    if game_tab.token:
        account_tokens[account.index] = game_tab.token
        log.info(f"[{account.index:02d}] Token registrado: {game_tab.token[:12]}...")
        if game_tab.api_url:
            config.API_URL = game_tab.api_url
            host   = game_tab.api_url.split("/")[2]
            domain = host.split(".", 1)[1]
            config.API_ORIGIN = f"https://wbgame.{domain}"

    log.info(f"[{account.index:02d}] Fluxo visual concluído — sistema pronto")
    active_tabs[account.index]     = game_tab
    active_accounts[account.index] = account


# ---------------------------------------------------------------------------
# ROTAÇÃO DE TOKEN — via API direta (sem sair do jogo)
# ---------------------------------------------------------------------------

async def rotate_token(account_index: int) -> str | None:
    """Chama launchUrl via httpx com kid armazenado + cookies frescos → reload_game → novo Token."""
    tab  = active_tabs.get(account_index)
    hdrs = account_launch_hdrs.get(account_index, {})
    if not tab:
        return None

    loop   = asyncio.get_event_loop()
    cookie = hdrs.get("cookie", "")
    kid    = hdrs.get("kid", "")

    if not cookie:
        log.warning(f"[{account_index:02d}] rotate_token: sem cookies — impossível rotar")
        return None

    _base = config.LOGIN_URL.rsplit("/login", 1)[0]
    req_headers = {
        "content-type": "application/json",
        "origin":        _base,
        "referer":       _base + "/",
        "cookie":        cookie,
    }
    if kid:
        req_headers["kid"] = kid

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp      = await client.post(_LAUNCH_URL, json=_LAUNCH_BODY, headers=req_headers)
            resp_json = resp.json()
    except Exception as exc:
        log.error(f"[{account_index:02d}] rotate_token: requisição falhou: {exc}")
        return None

    data      = resp_json.get("data", "")
    game_url  = data if isinstance(data, str) else (data or {}).get("url", "")

    if not game_url or not game_url.startswith("http"):
        log.error(f"[{account_index:02d}] rotate_token: resposta inválida (kid={'present' if kid else 'absent'}) — {resp_json}")
        return None

    log.info(f"[{account_index:02d}] Nova URL obtida — recarregando jogo...")
    ok = await loop.run_in_executor(None, tab.reload_game, game_url)

    if ok and tab.token:
        account_tokens[account_index] = tab.token
        if tab.api_url:
            config.API_URL    = tab.api_url
            host   = tab.api_url.split("/")[2]
            domain = host.split(".", 1)[1]
            config.API_ORIGIN = f"https://wbgame.{domain}"
        log.info(f"[{account_index:02d}] Token rotacionado: {tab.token[:12]}...")
        return tab.token

    log.warning(f"[{account_index:02d}] rotate_token: reload_game não capturou token")
    return None


# ---------------------------------------------------------------------------
# REENTRADA NO JOGO — mesma conta, sem novo login
# ---------------------------------------------------------------------------

async def reenter_game(account_index: int) -> str | None:
    """Volta ao GameList e reentra no jogo com a mesma conta — sem novo OAuth."""
    old_tab = active_tabs.pop(account_index, None)
    if old_tab:
        try:
            old_tab.stop()
        except Exception:
            pass
    account_tokens.pop(account_index, None)

    port    = 9300 + account_index
    account = active_accounts.get(account_index)
    if not account:
        log.error(f"[{account_index:02d}] reenter_game: conta não encontrada")
        return None

    log.info(f"[{account_index:02d}] Sessão morta — reentrando no jogo...")

    while account_index in _restarting_accounts:
        await asyncio.sleep(2)

    # Se _restart_chrome (health/mem_limiter) já restaurou a aba, retorna o token
    if account_index in active_tabs:
        tok = account_tokens.get(account_index)
        if tok:
            log.info(f"[{account_index:02d}] Sessão já restaurada pelo health monitor — token reutilizado")
            return tok

    async with _reenter_sem:
        return await _reenter_game_inner(account_index, port, account)


async def _reenter_game_inner(account_index: int, port: int, account: "Account") -> "str | None":
    try:
        _pause_cpulimit_for_port(port)
        try:
            browser = await _playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=30_000
            )
        except Exception as _cdp_err:
            log.warning(f"[{account_index:02d}] connect_over_cdp falhou ({type(_cdp_err).__name__}) — reiniciando Chrome")
            _resume_cpulimit_for_port(port)
            await _spawn_chrome(account)
            _pause_cpulimit_for_port(port)
            browser = await _playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=30_000
            )
        _resume_cpulimit_for_port(port)
        ctx  = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        _pw_launch_headers: dict = {}
        def _on_req(request):
            if "thirdGame/launchUrl" in request.url and not _pw_launch_headers:
                h = request.headers
                cookie = h.get("cookie")
                kid    = h.get("kid")
                if cookie:
                    _pw_launch_headers["cookie"] = cookie
                    if kid:
                        _pw_launch_headers["kid"] = kid
        page.on("request", _on_req)

        captured: dict = {}
        async def _intercept(route):
            response = await route.fetch()
            try:
                body = await response.json()
                data = body.get("data", "")
                if isinstance(data, str) and data.startswith("http"):
                    captured["game_url"] = data
            except Exception:
                pass
            await route.fulfill(response=response)
        await page.route("**/thirdGame/launchUrl", _intercept)

        # Navega para blank primeiro para liberar recursos WebGL da sessão anterior
        try:
            await page.goto("about:blank", timeout=5_000)
        except Exception:
            pass

        await page.goto(config.GAMELIST_URL, timeout=config.NAV_TIMEOUT * 1000)
        await page.wait_for_load_state("networkidle")

        try:
            close_btn = page.locator(".closeImg")
            await close_btn.wait_for(state="visible", timeout=4_000)
            await close_btn.click()
        except Exception:
            pass

        game_card = page.locator(f'.bgImg:has(img[src*="{config.GAME_IMAGE_URL_FRAGMENT}"])').first
        await game_card.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT * 1000)
        await game_card.dispatch_event("click")

        play_btn = page.locator('span.mode:has-text("JOGUE"), button:has-text("JOGUE")').first
        await play_btn.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT * 1000)
        await play_btn.dispatch_event("click")
        log.info(f"[{account_index:02d}] JOGUE clicado")

        for _ in range(60):
            if "game_url" in captured:
                break
            await asyncio.sleep(0.5)

        _casino_host = config.LOGIN_URL.split("/")[2]
        game_url = captured.get("game_url", "")
        if not game_url or _casino_host in game_url:
            log.error(f"[{account_index:02d}] reenter_game: URL do jogo inválida")
            await browser.close()
            return None

        import requests as req
        resp     = req.get(f"http://127.0.0.1:{port}/json")
        tabs     = resp.json()
        target_id = next((t["id"] for t in tabs if page.url.split("#")[0] in t["url"]), tabs[0]["id"])

        if _pw_launch_headers:
            account_launch_hdrs[account_index] = _pw_launch_headers

        await browser.close()

        loop     = asyncio.get_event_loop()
        game_tab = await loop.run_in_executor(None, take_over_tab, port, target_id, BYPASS_JS)
        if not game_tab:
            log.error(f"[{account_index:02d}] reenter_game: falha ao assumir aba pychrome")
            return None

        visual_ok = await loop.run_in_executor(None, _run_game_flow, game_tab, account_index)
        if not visual_ok:
            game_tab.stop()
            return None

        if game_tab.token:
            account_tokens[account_index] = game_tab.token
            active_tabs[account_index]    = game_tab
            if game_tab.api_url:
                config.API_URL    = game_tab.api_url
                host   = game_tab.api_url.split("/")[2]
                domain = host.split(".", 1)[1]
                config.API_ORIGIN = f"https://wbgame.{domain}"
            game_tab.throttle_rendering()
            log.info(f"[{account_index:02d}] Jogo reentrado — token: {game_tab.token[:12]}...")
            return game_tab.token

        log.warning(f"[{account_index:02d}] reenter_game: token não capturado")
        return None

    except Exception as exc:
        log.error(f"[{account_index:02d}] reenter_game falhou: {exc}")
        _kill_chrome_tree(port)
        return None
    finally:
        _resume_cpulimit_for_port(port)


# ---------------------------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# ROTAÇÃO DE CONTA — nova conta + login + jogo (sem logout)
# ---------------------------------------------------------------------------

async def rotate_account(account_index: int) -> str | None:
    """Troca para nova conta Google, inicializa o jogo e retorna novo token."""
    # Para tab pychrome atual
    tab = active_tabs.pop(account_index, None)
    if tab:
        try:
            tab.stop()
        except Exception:
            pass
    account_tokens.pop(account_index, None)

    port = 9300 + account_index
    new_account = pop_next_account(reuse_index=account_index)
    if new_account is None:
        log.error(f"[{account_index:02d}] Sem contas disponíveis para rotação")
        return None
    active_accounts[account_index] = new_account
    log.info(f"[{account_index:02d}] Próxima conta: {new_account.login}")

    try:
        browser = await _playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx  = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Captura headers de launch
        _pw_launch_headers: dict = {}
        def _on_req(request):
            if "thirdGame/launchUrl" in request.url and not _pw_launch_headers:
                h = request.headers
                cookie = h.get("cookie")
                kid    = h.get("kid")
                if cookie:
                    _pw_launch_headers["cookie"] = cookie
                    if kid:
                        _pw_launch_headers["kid"] = kid
        page.on("request", _on_req)

        captured: dict = {}
        async def _intercept(route):
            response = await route.fetch()
            try:
                body = await response.json()
                data = body.get("data", "")
                if isinstance(data, str) and data.startswith("http"):
                    captured["game_url"] = data
            except Exception:
                pass
            await route.fulfill(response=response)
        await page.route("**/thirdGame/launchUrl", _intercept)

        # Google OAuth com nova conta
        ok = await _google_login(page, new_account, account_index)
        if not ok:
            log.error(f"[{account_index:02d}] Google OAuth pós-rotação falhou")
            release_account(new_account)
            await browser.close()
            return None

        # Navega para GameList, encontra o jogo e clica JOGUE
        await page.goto(config.GAMELIST_URL, timeout=config.NAV_TIMEOUT * 1000)
        await page.wait_for_load_state("networkidle")
        try:
            close_btn = page.locator(".closeImg")
            await close_btn.wait_for(state="visible", timeout=4_000)
            await close_btn.click()
        except Exception:
            pass
        game_card = page.locator(f'.bgImg:has(img[src*="{config.GAME_IMAGE_URL_FRAGMENT}"])').first
        await game_card.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT * 1000)
        await game_card.dispatch_event("click")
        play_btn = page.locator('span.mode:has-text("JOGUE"), button:has-text("JOGUE")').first
        await play_btn.wait_for(state="visible", timeout=config.SELECTOR_TIMEOUT * 1000)
        await play_btn.dispatch_event("click")

        for _ in range(60):
            if "game_url" in captured:
                break
            await asyncio.sleep(0.5)

        import requests as req
        resp_tabs = req.get(f"http://127.0.0.1:{port}/json")
        tabs_json = resp_tabs.json()
        target_id = next(
            (t["id"] for t in tabs_json if page.url.split("#")[0] in t["url"]),
            tabs_json[0]["id"],
        )

        if _pw_launch_headers:
            account_launch_hdrs[account_index] = _pw_launch_headers

        await browser.close()

        loop = asyncio.get_event_loop()
        game_tab = await loop.run_in_executor(None, take_over_tab, port, target_id, BYPASS_JS)
        if game_tab is None:
            return None

        visual_ok = await loop.run_in_executor(None, _run_game_flow, game_tab, account_index)
        if not visual_ok:
            game_tab.stop()
            return None

        if game_tab.token:
            account_tokens[account_index] = game_tab.token
            active_tabs[account_index]    = game_tab
            if game_tab.api_url:
                config.API_URL = game_tab.api_url
                host   = game_tab.api_url.split("/")[2]
                domain = host.split(".", 1)[1]
                config.API_ORIGIN = f"https://wbgame.{domain}"
            game_tab.throttle_rendering()
            log.info(f"[{account_index:02d}] Rotação completa → token: {game_tab.token[:12]}...")
            return game_tab.token

    except Exception as exc:
        log.error(f"[{account_index:02d}] rotate_account falhou: {exc}")
    return None


# ---------------------------------------------------------------------------
# KEEPALIVE — mantém abas vivas indefinidamente
# ---------------------------------------------------------------------------

async def _keepalive_loop() -> None:
    tick = 0
    while True:
        await asyncio.sleep(30)
        tick += 1

        # Ping HTTP a cada 30s — mantém conexão TCP/TLS aquecida com o servidor
        await http_keepalive()

        # Keepalive + throttle a cada 60s com reconexão automática se necessário
        if tick % 2 == 0:
            loop = asyncio.get_event_loop()
            for idx, tab in list(active_tabs.items()):
                try:
                    ok = await loop.run_in_executor(None, tab.reapply_throttle_sync)
                    if not ok:
                        log.warning(f"[{idx:02d}] Throttle falhou — reconectando pychrome...")
                        reconnected = await loop.run_in_executor(None, tab.reconnect)
                        if reconnected:
                            await loop.run_in_executor(None, tab.reapply_throttle_sync)
                            log.info(f"[{idx:02d}] Reconexão OK — throttle reaplicado")
                    else:
                        # Keepalive: fetch real (HEAD na GameList) + evento de mouse
                        # O fetch gera tráfego que o servidor vê como atividade
                        try:
                            tab._tab.Runtime.evaluate(expression="""
                                (function(){
                                    try { fetch('/gameList?flag=103', { method: 'HEAD', cache: 'no-cache' }); } catch(e) {}
                                    document.dispatchEvent(new Event('mousemove'));
                                })();
                            """)
                        except Exception:
                            pass
                except Exception:
                    log.warning(f"[{idx:02d}] Keepalive falhou — aba pode ter caído")

            # GC forçado a cada 5 minutos (tick a cada 30s → 10 ticks = 5min)
            if tick % 10 == 0:
                for idx, tab in list(active_tabs.items()):
                    try:
                        await loop.run_in_executor(None, tab.gc)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# CPU LIMITER — limita renderers via cpulimit com suporte a pause/resume
# ---------------------------------------------------------------------------

def _get_renderer_port(renderer_pid: int) -> int | None:
    """Retorna a porta de debug do Chrome pai de um renderer."""
    try:
        import psutil
        parent = psutil.Process(renderer_pid).parent()
        if parent:
            for arg in (parent.cmdline() or []):
                if arg.startswith("--remote-debugging-port="):
                    return int(arg.split("=", 1)[1])
    except Exception:
        pass
    return None


def _pause_cpulimit_for_port(port: int) -> None:
    """Mata cpulimit e resume renderers de um Chrome antes de operação Playwright."""
    import os
    import signal
    _cpulimit_paused_ports.add(port)
    to_remove = [p for p in list(_limited_renderers) if _get_renderer_port(p) == port]
    for rpid in to_remove:
        try:
            _limited_renderers[rpid].kill()
        except Exception:
            pass
        try:
            os.kill(rpid, signal.SIGCONT)
        except Exception:
            pass
        del _limited_renderers[rpid]
    if to_remove:
        log.info(f"[cpu_limiter] Porta {port} suspensa — {len(to_remove)} renderer(s) retomado(s)")


def _resume_cpulimit_for_port(port: int) -> None:
    """Retoma cpulimit para renderers de um Chrome após operação Playwright."""
    _cpulimit_paused_ports.discard(port)
    log.info(f"[cpu_limiter] Porta {port} retomada — cpulimit reaplicado em até 5s")


async def _cpu_limiter_loop() -> None:
    """Aplica cpulimit a cada processo --type=renderer do Chrome. Só roda em Linux."""
    if sys.platform != "linux":
        return

    try:
        result = subprocess.run(["which", "cpulimit"], capture_output=True)
        if result.returncode != 0:
            log.warning("[cpu_limiter] cpulimit não encontrado — instale com: apt install cpulimit")
            return
    except Exception:
        log.warning("[cpu_limiter] não foi possível verificar cpulimit")
        return

    try:
        import psutil
    except ImportError:
        log.warning("[cpu_limiter] psutil não disponível")
        return

    log.info(f"[cpu_limiter] Ativo — renderer={config.CPU_LIMIT_RENDERER}% | gpu={config.CPU_LIMIT_GPU}%")

    while True:
        await asyncio.sleep(5)
        try:
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    if "chrome" not in (proc.info["name"] or "").lower():
                        continue
                    cmdline = proc.info["cmdline"] or []
                    if "--type=renderer" in cmdline:
                        limit = config.CPU_LIMIT_RENDERER
                        ptype = "renderer"
                    elif "--type=gpu-process" in cmdline:
                        limit = config.CPU_LIMIT_GPU
                        ptype = "gpu"
                    else:
                        continue
                    pid = proc.info["pid"]
                    if pid in _limited_renderers:
                        continue
                    port = _get_renderer_port(pid)
                    if port and port in _cpulimit_paused_ports:
                        continue
                    proc_obj = subprocess.Popen(
                        ["cpulimit", "-p", str(pid), "-l", str(limit), "--"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    _limited_renderers[pid] = proc_obj
                    log.info(f"[cpu_limiter] PID {pid} ({ptype}, porta {port}) → {limit}% CPU")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            # Remove PIDs mortos do tracking
            dead = {p for p in _limited_renderers if not psutil.pid_exists(p)}
            for p in dead:
                del _limited_renderers[p]
        except Exception as e:
            log.warning(f"[cpu_limiter] erro: {e}")


# ---------------------------------------------------------------------------
# MEMORY LIMITER — reinicia Chrome quando RSS ultrapassa o limite configurado
# ---------------------------------------------------------------------------

def _kill_chrome_tree(port: int) -> None:
    """Mata Chrome + todos os filhos recursivamente pelo porto de debug."""
    import os, signal
    # Remove tracking de uptime para as portas que serão mortas
    for idx in list(_chrome_start_time.keys()):
        if 9300 + idx == port:
            del _chrome_start_time[idx]
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if proc.info["name"] and "chrome" in proc.info["name"].lower():
                    if any(f"--remote-debugging-port={port}" in (a or "") for a in (proc.info["cmdline"] or [])):
                        parent = psutil.Process(proc.info["pid"])
                        children = parent.children(recursive=True)
                        for child in children:
                            try:
                                child.kill()
                            except Exception:
                                pass
                        try:
                            parent.kill()
                        except Exception:
                            pass
                        psutil.wait_procs(children + [parent], timeout=5)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except ImportError:
        try:
            subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"], timeout=10)
        except Exception:
            pass


def _get_chrome_rss(account_index: int) -> int:
    """Retorna RSS total em MB do Chrome + todos os filhos (renderers, GPU, etc.)."""
    try:
        import psutil
        popen = _chrome_procs.get(account_index)
        if not popen:
            return 0
        try:
            p = psutil.Process(popen.pid)
        except psutil.NoSuchProcess:
            del _chrome_procs[account_index]
            return 0
        total = p.memory_info().rss
        for child in p.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return total // (1024 * 1024)
    except Exception:
        return 0


async def _restart_chrome(account_index: int) -> None:
    """Mata Chrome, relança na mesma porta e reentra no jogo sem novo login."""
    if account_index in _restarting_accounts:
        return
    _restarting_accounts.add(account_index)
    try:
        account = active_accounts.get(account_index)
        if not account:
            log.error(f"[mem_limiter] [{account_index:02d}] Conta não encontrada")
            return
        log.warning(f"[mem_limiter] [{account_index:02d}] Reiniciando Chrome por excesso de RAM...")
        tab = active_tabs.pop(account_index, None)
        if tab:
            try:
                tab.stop()
            except Exception:
                pass
        account_tokens.pop(account_index, None)
        await _spawn_chrome(account)
        port = 9300 + account_index
        new_token = await _reenter_game_inner(account_index, port, account)
        if new_token:
            log.info(f"[mem_limiter] [{account_index:02d}] Reinício OK — {_get_chrome_rss(account_index)}MB RSS")
        else:
            log.error(f"[mem_limiter] [{account_index:02d}] Reinício falhou")
    except Exception as e:
        log.error(f"[mem_limiter] [{account_index:02d}] _restart_chrome: {e}")
    finally:
        _restarting_accounts.discard(account_index)


async def _memory_limiter_loop() -> None:
    """Monitora RSS total por instância Chrome; reinicia quando acima do limite."""
    if sys.platform != "linux":
        return
    try:
        import psutil  # noqa: F401
    except ImportError:
        log.warning("[mem_limiter] psutil não disponível")
        return
    limit = config.MEMORY_LIMIT_BROWSER_MB
    log.info(f"[mem_limiter] Ativo — limite {limit}MB por instância Chrome")
    while True:
        await asyncio.sleep(30)
        try:
            for account_index in list(_chrome_procs.keys()):
                if account_index in _restarting_accounts:
                    continue
                rss = _get_chrome_rss(account_index)
                if rss == 0:
                    continue
                if rss > limit:
                    log.warning(f"[mem_limiter] [{account_index:02d}] RSS {rss}MB > {limit}MB — reiniciando")
                    asyncio.create_task(_restart_chrome(account_index))
                elif rss > int(limit * 0.85):
                    log.info(f"[mem_limiter] [{account_index:02d}] RSS {rss}MB ({rss / limit * 100:.0f}% do limite)")
        except Exception as e:
            log.warning(f"[mem_limiter] erro: {e}")


async def _health_monitor_loop() -> None:
    """Detecta quando perdeu varias contas e reinicia as que morreram.
    Também reinicia Chrome com mais de CHROME_MAX_AGE_HOURS para evitar acúmulo de GPU."""
    log.info("[health] Monitor ativo — verifica contas a cada 60s")
    max_age_s = config.CHROME_MAX_AGE_HOURS * 3600
    while True:
        await asyncio.sleep(60)
        try:
            active = len(active_tabs)
            total  = len(active_accounts)
            if total == 0:
                continue
            now = asyncio.get_event_loop().time()
            # Reinicia Chrome que passou do tempo máximo de vida (acúmulo GPU/WebGL)
            for idx in list(_chrome_start_time.keys()):
                age_h = (now - _chrome_start_time[idx]) / 3600
                if age_h >= config.CHROME_MAX_AGE_HOURS:
                    if idx not in _restarting_accounts and idx in active_accounts:
                        log.warning(f"[health] [{idx:02d}] Chrome com {age_h:.1f}h de uptime — reiniciando...")
                        asyncio.create_task(_restart_chrome(idx))
            # Quando menos de 30% das contas estão ativas, reinicia as mortas
            if active < total * 0.3:
                log.warning(f"[health] Só {active}/{total} contas ativas — reiniciando as mortas...")
                for idx in list(active_accounts.keys()):
                    if idx not in active_tabs and idx not in _restarting_accounts:
                        log.info(f"[health] Reiniciando conta {idx:02d}...")
                        asyncio.create_task(_restart_chrome(idx))
            # Limpa Chrome fantasma (processo morto sem aba ativa)
            for idx in list(_chrome_procs.keys()):
                if idx not in active_tabs and idx not in _restarting_accounts:
                    rss = _get_chrome_rss(idx)
                    if rss == 0:
                        # Processo já morreu — tenta reiniciar se a conta existe
                        if idx in active_accounts:
                            log.info(f"[health] Chrome {idx:02d} morto — reiniciando...")
                            asyncio.create_task(_restart_chrome(idx))
        except Exception as e:
            log.warning(f"[health] erro: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def _parse_args() -> tuple["int | None", "int | None"]:
    """Lê argumentos opcionais de range da linha de comando.

    Uso:
        python orchestrator.py          → todas as contas (até MAX_ACCOUNTS)
        python orchestrator.py 1 5      → account_01 até account_05
        python orchestrator.py 6 10     → account_06 até account_10
    """
    args = sys.argv[1:]
    if len(args) == 0:
        return None, None
    if len(args) == 2:
        try:
            start = int(args[0])
            end   = int(args[1])
            if start >= 1 and end >= start:
                return start, end
        except ValueError:
            pass
    print(
        "Uso: python orchestrator.py [inicio fim]\n"
        "  Exemplos:\n"
        "    python orchestrator.py          # todas as contas\n"
        "    python orchestrator.py 1 5      # account_01 até account_05\n"
        "    python orchestrator.py 6 10     # account_06 até account_10\n",
        file=sys.stderr,
    )
    sys.exit(1)


async def main() -> None:
    start_idx, end_idx = _parse_args()

    if start_idx is not None and end_idx is not None:
        accounts = load_accounts_by_range(start_idx, end_idx)
        log.info(
            f"{len(accounts)} conta(s) carregadas "
            f"[range account_{start_idx:02d} → account_{end_idx:02d}]"
        )
    else:
        accounts = load_accounts_from_txt(limit=config.MAX_ACCOUNTS)
        log.info(f"{len(accounts)} conta(s) carregadas de accounts.txt")

    if not accounts:
        log.error("Nenhuma conta encontrada — verifique accounts.txt e o range informado")
        return

    async with async_playwright() as playwright:
        global _playwright
        _playwright = playwright

        for batch_start in range(0, len(accounts), config.LAUNCH_BATCH):
            batch = accounts[batch_start : batch_start + config.LAUNCH_BATCH]
            tasks = [
                asyncio.create_task(setup_account(playwright, acc, stagger=i))
                for i, acc in enumerate(batch)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for acc, result in zip(batch, results):
                if isinstance(result, Exception):
                    log.error(f"[{acc.index:02d}] Setup falhou: {result}")

            for acc in batch:
                tab = active_tabs.get(acc.index)
                if tab:
                    tab.throttle_rendering()
            log.info(f"Lote {batch_start // config.LAUNCH_BATCH + 1} concluído ({len(active_tabs)} prontas)")

        ready = len(active_tabs)
        log.info(f"Setup completo — {ready}/{len(accounts)} contas prontas | {len(account_tokens)} token(s) capturado(s)")

        if ready == 0:
            log.error("Nenhuma conta pronta.")
            await close_session()
            return

        await warm_up()
        asyncio.create_task(_keepalive_loop())
        asyncio.create_task(_cpu_limiter_loop())
        asyncio.create_task(_memory_limiter_loop())
        asyncio.create_task(_health_monitor_loop())
        log.info("Sistema pronto — aguardando códigos...")

        tester_task = asyncio.create_task(run_testers(
            account_tokens,
            active_accounts,
            rotate_fn=rotate_account,
            broadcast_fn=broadcast_promo,
            stale_fn=reenter_game,
        ))

        try:
            await tester_task
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Encerrado pelo usuário.")
        finally:
            await close_session()
            for proc in _chrome_procs.values():
                try:
                    proc.terminate()
                except Exception:
                    pass


if __name__ == "__main__":
    asyncio.run(main())

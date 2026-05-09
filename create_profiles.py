"""
create_profiles.py — Faz login Google em cada conta e salva o perfil Chrome.
Execute no Windows. Depois copie a pasta profiles/ para a VPS.

Uso:
    python create_profiles.py
"""

import asyncio
import logging
import subprocess
import sys
from pathlib import Path

import requests as _http
from playwright.async_api import async_playwright

import config
from account_manager import load_accounts_from_txt, Account, PROFILES_DIR, ACCOUNTS_TXT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("profiles")

_CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
CHROME_EXE = next((p for p in _CHROME_PATHS if Path(p).exists()), None)


async def _logado_check(page, timeout=15.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await page.locator('a[href="/deposit"]').count() > 0:
                return True
            # Popup de Vinculacao (primeiro login)
            if await page.locator('.van-popup--center').filter(has_text="Vinculação").count() > 0:
                return True
            # Tela de cadastro CPF (primeiro login apos Google OAuth)
            if await page.locator('input[placeholder*="CPF"], input[name*="cpf"], label:has-text("CPF")').count() > 0:
                return True
            if await page.locator('.van-field:has-text("CPF"), .van-cell:has-text("CPF")').count() > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return False


async def _google_login(page, account: Account) -> bool:
    for attempt in range(1, 4):
        try:
            await page.goto(config.LOGIN_URL, timeout=config.LOGIN_TIMEOUT * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            async with page.context.expect_page(timeout=15_000) as popup_info:
                await page.locator(config.GOOGLE_BTN_SEL).first.click()
            popup = await popup_info.value
            try:
                await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:
                pass

            log.info(f"[{account.index:02d}] Popup Google aberto (tentativa {attempt})")

            _chooser_sel = f'[data-identifier="{account.login}"], [data-email="{account.login}"]'
            _email_sel   = 'input[type="email"]'
            try:
                await popup.wait_for_selector(f'{_chooser_sel}, {_email_sel}', timeout=12_000)
            except Exception:
                pass

            if await popup.locator(_chooser_sel).count() > 0:
                await popup.locator(_chooser_sel).first.click()
                log.info(f"[{account.index:02d}] Conta selecionada no chooser")
                await asyncio.sleep(1.0)
            else:
                for use_other in [
                    '#use_another_account',
                    'li:has-text("Use another account")',
                    'li:has-text("Usar outra conta")',
                ]:
                    try:
                        btn = popup.locator(use_other)
                        if await btn.count() > 0:
                            await btn.first.click()
                            await asyncio.sleep(1.5)
                            break
                    except Exception:
                        pass

                await popup.locator('input[type="email"]').fill(account.login, timeout=10_000)
                await popup.locator(
                    '#identifierNext, button:has-text("Next"), button:has-text("Proximo")'
                ).first.click(timeout=5_000)
                await popup.wait_for_selector('input[type="password"]:not([aria-hidden="true"])', timeout=15_000)
                await popup.locator('input[type="password"]:not([aria-hidden="true"])').fill(account.senha)
                await popup.locator(
                    '#passwordNext, button:has-text("Next"), button:has-text("Proximo")'
                ).first.click(timeout=5_000)

            for _ in range(6):
                if popup.is_closed():
                    break
                for btn_text in ['button:has-text("I understand")', 'button:has-text("Entendi")']:
                    try:
                        btn = popup.locator(btn_text)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=3_000)
                            await asyncio.sleep(0.8)
                    except Exception:
                        pass
                clicou = False
                for btn_text in ['button:has-text("Continue")', 'button:has-text("Continuar")', 'button:has-text("Permitir")']:
                    try:
                        btn = popup.locator(btn_text)
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

            if not popup.is_closed():
                try:
                    await popup.wait_for_event("close", timeout=30_000)
                except Exception:
                    pass

            if await _logado_check(page, timeout=15.0):
                log.info(f"[{account.index:02d}] Login OK")
                return True

            log.warning(f"[{account.index:02d}] Tentativa {attempt}/3 falhou")

        except Exception as exc:
            log.warning(f"[{account.index:02d}] Tentativa {attempt}/3 erro: {exc}")
            await asyncio.sleep(2.0)

    return False


async def process_account(account: Account) -> None:
    if not CHROME_EXE:
        log.error("Chrome nao encontrado.")
        return

    port = 19300 + account.index
    account.profile_dir.mkdir(parents=True, exist_ok=True)

    # Remove lock files de execucao anterior
    for lock in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        lf = account.profile_dir / lock
        if lf.exists():
            try:
                lf.unlink()
            except Exception:
                pass

    args = [
        CHROME_EXE,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={account.profile_dir}",
        "--no-proxy-server",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--window-size=400,700",
        "--no-first-run",
        "--disable-default-apps",
    ]

    proc = subprocess.Popen(args)

    try:
        # Aguarda Chrome iniciar
        for _ in range(40):
            try:
                r = _http.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        async with async_playwright() as pw:
            browser = await pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx  = browser.contexts[0] if browser.contexts else await browser.new_context(locale="pt-BR")
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()

            # Verifica se ja tem sessao salva
            await page.goto(config.GAMELIST_URL, timeout=config.NAV_TIMEOUT * 1000)
            try:
                await page.wait_for_load_state("networkidle", timeout=10_000)
            except Exception:
                pass

            if "/login" not in page.url and await _logado_check(page, timeout=5.0):
                log.info(f"[{account.index:02d}] {account.login} — sessao ja existe, pulando")
            else:
                log.info(f"[{account.index:02d}] {account.login} — fazendo login...")
                ok = await _google_login(page, account)
                if not ok:
                    log.error(f"[{account.index:02d}] Login falhou — perfil nao salvo")
                    await browser.close()
                    return

            # Garante que cookies foram salvos no disco
            await asyncio.sleep(2.0)
            await browser.close()
            log.info(f"[{account.index:02d}] Perfil salvo em: {account.profile_dir}")

    finally:
        proc.terminate()


def _next_profile_index() -> int:
    """Retorna o proximo indice disponivel baseado nas pastas de perfil existentes."""
    if not PROFILES_DIR.exists():
        return 0
    max_idx = -1
    for d in PROFILES_DIR.iterdir():
        if d.is_dir() and d.name.startswith("account_"):
            try:
                idx = int(d.name.split("_", 1)[1])
                if idx > max_idx:
                    max_idx = idx
            except ValueError:
                pass
    return max_idx + 1


async def main() -> None:
    raw = []
    if not ACCOUNTS_TXT.exists():
        log.error("accounts.txt nao encontrado")
        return

    for line in ACCOUNTS_TXT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        email = parts[1].strip()
        senha = parts[2].strip()
        if email:
            raw.append((email, senha))

    if not raw:
        log.error("Nenhuma conta em accounts.txt")
        return

    start_idx = _next_profile_index()
    log.info(f"Proximo indice disponivel: {start_idx:02d}")
    log.info(f"{len(raw)} conta(s) para processar")

    created = []
    for i, (email, senha) in enumerate(raw):
        idx = start_idx + i
        account = Account(index=idx, login=email, senha=senha)
        log.info(f"--- Conta {account.index:02d}: {account.login} ---")
        await process_account(account)
        created.append(account)
        await asyncio.sleep(1.0)

    # Atualiza accounts.txt com os indices corretos (sem sobrescrever existentes)
    existing = []
    if ACCOUNTS_TXT.exists():
        existing = [
            l.strip() for l in ACCOUNTS_TXT.read_text(encoding="utf-8").splitlines()
            if l.strip()
        ]
    # Remove linhas antigas desses emails (caso existam com indice errado)
    created_emails = {a.login.lower() for a in created}
    existing = [l for l in existing if not any(
        e in l.lower() for e in created_emails
    )]
    # Adiciona novas entradas com indice correto
    for acc in created:
        existing.append(f"{acc.index:02d}|{acc.login}|{acc.senha}")
    ACCOUNTS_TXT.write_text("\n".join(existing) + "\n", encoding="utf-8")
    log.info(f"accounts.txt atualizado com {len(created)} nova(s) conta(s)")

    log.info("Concluido. Copie a pasta profiles/ para a VPS:")
    log.info("  scp -r profiles/ root@<IP_VPS>:/home/betapp/")


if __name__ == "__main__":
    asyncio.run(main())

"""
code_tester.py — Testa códigos em paralelo. Quando código válido: broadcast + rotaciona conta.
Geração sequencial: v555aaaa → v555aaab → ... → v5559999

Coordenação multi-usuário via Firebase (opcional):
  - Se FIREBASE_URL configurado: contador atômico compartilhado — sem sobreposição entre instâncias
  - Se não configurado: modo local com tested_position.txt
"""

import asyncio
import datetime
import logging
from pathlib import Path

import config
from backend_redeemer import test_code

log = logging.getLogger("timbet")

_stop = asyncio.Event()
_attempts = 0
_valid_codes: list[str] = []

# --- Modo local ---
_index_lock    = asyncio.Lock()
_current_index = 0
_TOTAL_CODES   = len(config.CODE_CHARSET) ** config.CODE_LENGTH
_POSITION_PATH  = Path(__file__).parent / "tested_position.txt"
_VALID_PATH     = Path(__file__).parent / "codigos_validos.txt"
_CONTAS_PATH    = Path(__file__).parent / "contas_criadas.txt"

# --- Modo Firebase ---
_firebase_ref   = None
_fb_lock        = asyncio.Lock()
_fb_batch_start = 0
_fb_batch_end   = 0  # exclusivo


def _init_firebase() -> bool:
    global _firebase_ref
    if not config.FIREBASE_URL or not config.FIREBASE_KEY_PATH:
        return False
    try:
        import firebase_admin
        from firebase_admin import credentials, db as fdb
        if not firebase_admin._apps:
            cred = credentials.Certificate(config.FIREBASE_KEY_PATH)
            firebase_admin.initialize_app(cred, {"databaseURL": config.FIREBASE_URL})
        _firebase_ref = fdb.reference("betapp_code_index")
        log.info("[firebase] Conectado — coordenação ativa entre instâncias")
        return True
    except Exception as e:
        log.warning(f"[firebase] Falha ao conectar: {e} — usando modo local")
        return False


async def _claim_batch() -> tuple[int, int] | None:
    """Reivindica atomicamente um lote de FIREBASE_BATCH índices no Firebase."""
    loop = asyncio.get_event_loop()

    def _do():
        def _txn(current):
            return (current or 0) + config.FIREBASE_BATCH
        new_val = _firebase_ref.transaction(_txn)
        return new_val - config.FIREBASE_BATCH, new_val

    try:
        return await loop.run_in_executor(None, _do)
    except Exception as e:
        log.error(f"[firebase] Falha ao reivindicar lote: {e}")
        return None


def _load_position() -> int:
    try:
        return int(_POSITION_PATH.read_text().strip())
    except Exception:
        return 0


def _save_position(index: int) -> None:
    try:
        _POSITION_PATH.write_text(str(index))
    except Exception as e:
        log.warning(f"[tester] Falha ao salvar posição: {e}")


def _index_to_code(index: int) -> str:
    charset = config.CODE_CHARSET
    base = len(charset)
    digits = []
    for _ in range(config.CODE_LENGTH):
        digits.append(charset[index % base])
        index //= base
    return config.CODE_PREFIX + "".join(reversed(digits))


async def _next_code() -> str | None:
    global _current_index, _fb_batch_start, _fb_batch_end

    if _firebase_ref is not None:
        async with _fb_lock:
            if _fb_batch_start >= _fb_batch_end:
                result = await _claim_batch()
                if result is None:
                    return None
                _fb_batch_start, _fb_batch_end = result
                log.debug(f"[firebase] Lote reivindicado: {_fb_batch_start}–{_fb_batch_end - 1}")
            idx = _fb_batch_start
            _fb_batch_start += 1
        if idx >= _TOTAL_CODES:
            return None
        return _index_to_code(idx)
    else:
        async with _index_lock:
            if _current_index >= _TOTAL_CODES:
                return None
            code = _index_to_code(_current_index)
            _current_index += 1
            if _current_index % 500 == 0:
                _save_position(_current_index)
            return code


def _save_valid_code(code: str, login: str, senha: str, http_status: int = 0, size: int = 0) -> None:
    try:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_VALID_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} | codigo={code} | login={login} | senha={senha} | HTTP={http_status} | size={size}b\n")
        with open(_CONTAS_PATH, "a", encoding="utf-8") as f:
            f.write(f"LOGIN: {login}\nSENHA: {senha}\n{'-'*20}\n")
        _valid_codes.append(code)
        sep = "=" * 55
        log.info(sep)
        log.info(f"  CODIGO VALIDO ENCONTRADO: {code}")
        log.info(f"  login={login}  |  HTTP={http_status}  |  size={size}b")
        log.info(sep)
    except Exception as e:
        log.error(f"Falha ao salvar código: {e}")


async def _worker(index: int, token: str, account, accounts: dict, rotate_fn=None, broadcast_fn=None, stale_fn=None) -> None:
    global _attempts
    consecutive_invalid = 0
    current_token = token

    while not _stop.is_set():
        if consecutive_invalid >= 2:
            log.debug(f"[{index:02d}] Prevenção de bloqueio — pausando {config.COOLDOWN_PREVENTION}s")
            await asyncio.sleep(config.COOLDOWN_PREVENTION)
            consecutive_invalid = 0

        code = await _next_code()
        if code is None:
            log.info(f"[{index:02d}] Espaço de códigos esgotado")
            _stop.set()
            break

        status, body, http_status = await test_code(current_token, code, index)
        _attempts += 1

        if _attempts % 50 == 0:
            pos = _fb_batch_start if _firebase_ref else _current_index
            pct = pos / _TOTAL_CODES * 100
            log.info(f"[tester] {_attempts} tentativas | posição ~{pos}/{_TOTAL_CODES} ({pct:.2f}%)")
            if _valid_codes:
                log.info(f"[tester] Codigos encontrados: {', '.join(_valid_codes)}")

        if status == "valid":
            login = account.login if account else "?"
            senha = account.senha if account else "?"
            body_size = len(body) if body else 0
            _save_valid_code(code, login, senha, http_status=http_status, size=body_size)

            if broadcast_fn:
                await broadcast_fn(code)

            if rotate_fn:
                log.info(f"[{index:02d}] Rotacionando conta...")
                new_token = await rotate_fn(index)
                if new_token:
                    current_token = new_token
                    account = accounts.get(index) or account  # atualiza para nova conta
                    consecutive_invalid = 0
                    continue
                log.warning(f"[{index:02d}] Rotação falhou — encerrando worker")

            break

        elif status == "rate_limited":
            log.warning(f"[{index:02d}] Bloqueio acionado — aguardando {config.COOLDOWN_RECOVERY}s")
            await asyncio.sleep(config.COOLDOWN_RECOVERY)
            consecutive_invalid = 0

        elif status == "stale":
            log.warning(f"[{index:02d}] Sessão morta (37b) — reentrando no jogo...")
            fn = stale_fn or rotate_fn
            if fn:
                for retry in range(3):
                    new_token = await fn(index)
                    if new_token:
                        current_token = new_token
                        consecutive_invalid = 0
                        log.info(f"[{index:02d}] Reentrada OK (tentativa {retry+1})")
                        break
                    backoff = 2 ** retry  # 1s, 2s, 4s
                    log.warning(f"[{index:02d}] Reentrada falhou ({retry+1}/3) — retry em {backoff}s")
                    await asyncio.sleep(backoff)
                else:
                    log.error(f"[{index:02d}] Reentrada falhou 3x — health monitor assumirá")
                    break
            else:
                await asyncio.sleep(5.0)

        elif status == "invalid":
            consecutive_invalid += 1

        else:
            await asyncio.sleep(1.0)


async def run_testers(tokens: dict, accounts: dict, rotate_fn=None, broadcast_fn=None, stale_fn=None) -> None:
    """Inicia workers paralelos — um por conta. Roda até esgotar códigos ou ser interrompido."""
    global _attempts, _current_index
    _stop.clear()
    _attempts = 0

    _init_firebase()
    _current_index = _load_position()

    if not tokens:
        log.warning("[tester] Nenhum token disponível")
        return

    mode = "rotação de conta" if rotate_fn else f"pausa {config.COOLDOWN_PREVENTION}s"
    coordenacao = "Firebase" if _firebase_ref else "local"
    log.info(
        f"[tester] Iniciando {len(tokens)} worker(s) | "
        f"charset={len(config.CODE_CHARSET)} chars | len={config.CODE_LENGTH} | "
        f"coordenação: {coordenacao} | proteção: {mode}"
    )

    tasks = [
        asyncio.create_task(_worker(idx, tok, accounts.get(idx), accounts, rotate_fn, broadcast_fn, stale_fn))
        for idx, tok in tokens.items()
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        _stop.set()
    finally:
        if _firebase_ref is None:
            _save_position(_current_index)

    log.info(f"[tester] Encerrado após {_attempts} tentativas")


def stop_testers() -> None:
    _stop.set()

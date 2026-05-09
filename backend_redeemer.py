"""
backend_redeemer.py — Disparo direto via HTTP/2 com payload Protobuf.
Sessão persistente + keepalive + retry imediato + DNS pre-aquecido.
"""

import asyncio
import datetime
import logging
import socket
from pathlib import Path

import httpx

import config

log = logging.getLogger("timbet")

_VALIDACAO_LOG_PATH = Path(__file__).parent / "validacao_log.txt"


def _log_validacao(code: str, http_status: int, size: int, decoded: str, hex_body: str) -> None:
    """Grava detalhes da validação em arquivo para consulta posterior."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    falso = "⚠️ FALSO POSITIVO" if http_status != 200 else ""
    try:
        with open(_VALIDACAO_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{ts} | codigo={code} | HTTP={http_status} | size={size}b | {falso}\n")
            f.write(f"  decoded=[{decoded}]\n")
            f.write(f"  hex={hex_body[:200]}\n")
            f.write("-" * 80 + "\n")
    except Exception:
        pass

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            http2=True,
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _client


async def close_session() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _build_headers(token: str) -> dict:
    origin = config.API_ORIGIN or "https://wbgame.nufzelo.tr"
    return {
        "accept": "*/*",
        "accept-language": "pt-BR,pt;q=0.9",
        "token": token,
        "origin": origin,
        "referer": origin + "/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
    }


def _decode_response(body: bytes) -> str:
    """Extrai strings legíveis de uma resposta protobuf para logar de forma clara."""
    parts = []
    i = 0
    while i < len(body) - 1:
        try:
            wire = body[i] & 0x07
            i += 1
            if wire == 2 and i < len(body):
                ln = body[i]; i += 1
                if 0 < ln <= 512 and i + ln <= len(body):
                    try:
                        t = body[i:i+ln].decode("utf-8")
                        if len(t) > 1 and all(c.isprintable() or c.isspace() for c in t):
                            parts.append(t)
                    except Exception:
                        pass
                    i += ln
            else:
                i += 1
        except Exception:
            i += 1
    return " | ".join(parts) if parts else body.hex()[:40]


def build_promo_payload(code: str) -> bytes:
    code_bytes = code.encode("utf-8")
    submsg = b"\x0A" + bytes([len(code_bytes)]) + code_bytes
    return b"\x08\x27\x12" + bytes([len(submsg)]) + submsg


async def _redeem_one(token: str, payload: bytes, index: int) -> bool:
    url = config.API_URL or ""
    if not url:
        return False
    headers = _build_headers(token)
    client = _get_client()
    for attempt in range(3):
        try:
            resp = await client.post(url, content=payload, headers=headers)
            decoded = _decode_response(resp.content)
            log.info(f"[{index:02d}] HTTP {resp.status_code} | {decoded}")
            return resp.status_code == 200
        except Exception as exc:
            if attempt == 2:
                log.error(f"[{index:02d}] Falhou após 3 tentativas: {exc}")
                return False
            log.warning(f"[{index:02d}] Retry {attempt + 1}: {exc}")
    return False


async def test_code(token: str, code: str, index: int) -> tuple[str, bytes, int]:
    """Testa um código. Retorna (status, body, http_status)."""
    url = config.API_URL or ""
    if not url:
        log.warning(f"[{index:02d}] API_URL não configurada — token não capturado ainda")
        return "error", b"", 0
    headers = _build_headers(token)
    client = _get_client()
    try:
        resp = await client.post(url, content=build_promo_payload(code), headers=headers)
        n = len(resp.content)
        if n > config.SUCCESS_THRESHOLD:
            status = "valid"
            decoded = _decode_response(resp.content)
            log.info(f"[{index:02d}] VALIDO  '{code}' | HTTP {resp.status_code} | {n}b | decoded=[{decoded}]")
            if resp.status_code != 200:
                log.warning(f"[{index:02d}] ⚠️ FALSO POSITIVO '{code}' — HTTP {resp.status_code} mas body > 100b | hex={resp.content.hex()[:120]}")
            _log_validacao(code, resp.status_code, n, decoded, resp.content.hex())
        elif n < config.RATE_LIMIT_THRESHOLD:
            status = "rate_limited"
            log.warning(f"[{index:02d}] BLOQUEIO '{code}' | {n}b")
        elif n <= config.STALE_RESPONSE_MAX:
            status = "stale"
            log.debug(f"[{index:02d}] stale '{code}' | {n}b")
        else:
            status = "invalid"
            log.info(f"[{index:02d}] invalido '{code}' | {n}b")
        return status, resp.content, resp.status_code
    except Exception as exc:
        log.warning(f"[{index:02d}] ERRO '{code}': {type(exc).__name__}: {exc}")
        return "error", b"", 0


async def broadcast_via_api(tokens: dict, code: str) -> int:
    if not tokens:
        log.warning("Nenhum token disponível")
        return 0

    payload = build_promo_payload(code)
    log.info(f"Payload: {payload.hex()}")

    tasks = [
        asyncio.create_task(_redeem_one(token, payload, idx))
        for idx, token in tokens.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if r is True)
    log.info(f"BROADCAST '{code}' — {ok}/{len(tokens)} conta(s)")
    return ok


async def http_keepalive() -> None:
    """Ping leve ao servidor para manter a conexão TCP/TLS ativa."""
    try:
        url = config.API_URL or ""
        if not url:
            return
        host = url.split("/")[2]
        client = _get_client()
        await client.get(
            f"https://{host}/",
            follow_redirects=False,
            timeout=httpx.Timeout(4.0),
        )
    except Exception:
        pass


async def warm_up() -> None:
    """Pré-resolve DNS e inicializa sessão HTTP/2 para o primeiro broadcast ser instantâneo."""
    loop = asyncio.get_event_loop()
    url = config.API_URL or ""
    if url:
        host = url.split("/")[2]
        try:
            await loop.run_in_executor(None, socket.getaddrinfo, host, 443)
            log.info(f"DNS pré-resolvido: {host}")
        except Exception as exc:
            log.warning(f"DNS warm-up falhou: {exc}")
    _get_client()
    log.info("Sessão HTTP/2 inicializada e pronta")

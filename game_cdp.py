"""
game_cdp.py — Controla a aba do jogo via pychrome (sem Runtime.enable).
Playwright gerencia login/HTTP; este módulo gerencia o jogo.
"""

import base64
import json
import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pychrome
import requests

log = logging.getLogger("timbet.cdp")

# ── Suprime JSONDecodeError do pychrome ao fechar WebSocket ──────────────────
_orig_excepthook = threading.excepthook
def _thread_excepthook(args):
    if args.exc_type is json.JSONDecodeError:
        return
    _orig_excepthook(args)
threading.excepthook = _thread_excepthook

# ── JS injetado ANTES de qualquer script da página ───────────────────────────
STEALTH_JS = """
(function() {
    // 1. Falsifica window.chrome
    if (!window.chrome) {
        try {
            Object.defineProperty(window, 'chrome', {
                value: { runtime: {}, loadTimes: function(){}, csi: function(){} },
                configurable: true,
                writable: true
            });
        } catch(e) {}
    }
    
    // 2. Remove navigator.webdriver (Chromium seta isso como true por causa da porta de debug)
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    } catch(e) {}
})();
"""

RESOURCES = Path(__file__).parent / "resources"
SCALES    = [1.0, 0.85, 0.70]
_template_cache: dict[str, list] = {}

_RAF_THROTTLE_JS = """
(function(){
    if(window._rafThrottled) return;
    window._rafThrottled = true;
    var _orig = window.requestAnimationFrame;
    var _last = 0;
    window.requestAnimationFrame = function(cb){
        return _orig.call(window, function(ts){
            if(ts - _last >= 1000){ _last = ts; cb(ts); }
            else {
                var _wait = Math.max(950 - (ts - _last), 0);
                window.setTimeout(function(){ window.requestAnimationFrame(cb); }, _wait);
            }
        });
    };
})();
"""

_RAF_REAPPLY_JS = """
(function(){
    window._rafThrottled = false;
    var _orig = (window.requestAnimationFrame.__orig__) || window.requestAnimationFrame;
    var _last = 0;
    window.requestAnimationFrame = function(cb){
        return _orig.call(window, function(ts){
            if(ts - _last >= 1000){ _last = ts; cb(ts); }
            else {
                var _wait = Math.max(950 - (ts - _last), 0);
                window.setTimeout(function(){ window.requestAnimationFrame(cb); }, _wait);
            }
        });
    };
    window.requestAnimationFrame.__orig__ = _orig;
    window._rafThrottled = true;
})();
"""


def _load_templates(name: str):
    if name in _template_cache:
        return _template_cache[name]
    p = RESOURCES / f"{name}.png"
    if not p.exists():
        return []
    tpl = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        return []
    scaled = []
    for s in SCALES:
        h, w = max(1, int(tpl.shape[0]*s)), max(1, int(tpl.shape[1]*s))
        scaled.append((cv2.resize(tpl, (w, h)), s))
    _template_cache[name] = scaled
    return scaled


def _match(screen_gray, templates, threshold):
    best_val, best_cx, best_cy = 0.0, 0, 0
    sh, sw = screen_gray.shape[:2]
    for tpl, _ in templates:
        th, tw = tpl.shape[:2]
        if th > sh or tw > sw:
            continue
        res = cv2.matchTemplate(screen_gray, tpl, cv2.TM_CCOEFF_NORMED)
        _, mv, _, ml = cv2.minMaxLoc(res)
        if mv > best_val:
            best_val, best_cx, best_cy = mv, ml[0] + tw//2, ml[1] + th//2
    return best_val, best_cx, best_cy


class GameTab:
    """Wrapper pychrome para a aba do jogo — sem Runtime, sem fingerprint."""

    def __init__(self, tab, port: int, bypass_js: str = "", tab_id: str = ""):
        self._tab     = tab
        self._port    = port
        self._tab_id  = tab_id
        self._bypass_js = bypass_js
        self.token: str | None = None
        self.api_url: str | None = None
        self.launch_headers: dict | None = None   # cookie + kid para rotação de token
        self._launch_request_ids: set = set()     # correlaciona requestId do launchUrl

        try:
            self._tab.Network.enable()
            self._tab.Network.requestWillBeSent         = self._on_request_will_be_sent
            # ExtraInfo contém os headers reais enviados ao servidor (inclui Cookie automático)
            self._tab.Network.requestWillBeSentExtraInfo = self._on_request_extra_info
        except Exception as e:
            log.warning(f"Não foi possível ativar Network intercept: {e}")

    def _run_bypass_now(self) -> None:
        if not self._bypass_js:
            return
        js = self._bypass_js
        tab = self._tab
        def _do():
            try:
                tab.Runtime.evaluate(expression=js)
                log.info("[bypass] JS injetado via Runtime.evaluate")
            except Exception as e:
                log.warning(f"[bypass] Runtime.evaluate falhou: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def _on_request_will_be_sent(self, **kwargs):
        req        = kwargs.get("request", {})
        url        = req.get("url", "")
        headers    = req.get("headers", {})
        request_id = kwargs.get("requestId", "")

        # Marca o requestId para capturar os cookies reais no ExtraInfo
        if "thirdGame/launchUrl" in url:
            self._launch_request_ids.add(request_id)
            # Kid é setado pelo JS (aparece aqui), Cookie vem no ExtraInfo
            kid = headers.get("kid") or headers.get("Kid")
            if kid and self.launch_headers is None:
                self.launch_headers = {"kid": kid}

        t = headers.get("token") or headers.get("Token")
        if t and len(t) >= 20 and "bet.app" not in url and not self.token:
            self.token = t
            # Extrai base + endpoint da URL real (ex: /lj500/req, /fg/req)
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                path_parts = parsed.path.strip("/").split("/")
                endpoint = "/" + "/".join(path_parts[:2]) if len(path_parts) >= 2 else "/fg/req"
                self.api_url = f"{parsed.scheme}://{parsed.netloc}{endpoint}"
            except Exception:
                self.api_url = "/".join(url.split("/")[:3]) + "/fg/req"
            log.info(f"[pychrome] Token capturado! {t} | endpoint: {self.api_url}")

    def _on_request_extra_info(self, **kwargs):
        """Headers reais incluindo Cookie adicionado automaticamente pelo browser."""
        request_id = kwargs.get("requestId", "")
        if request_id not in self._launch_request_ids or self.launch_headers and "cookie" in self.launch_headers:
            return

        headers = kwargs.get("headers", {})
        cookie  = headers.get("cookie") or headers.get("Cookie")
        kid     = headers.get("kid")    or headers.get("Kid")

        if cookie:
            existing = self.launch_headers or {}
            self.launch_headers = {"cookie": cookie}
            if kid or existing.get("kid"):
                self.launch_headers["kid"] = kid or existing["kid"]
            self._launch_request_ids.discard(request_id)
            log.info(f"[pychrome] launch_headers capturados via ExtraInfo (kid={'present' if self.launch_headers.get('kid') else 'absent'})")

    # ── Screenshot ────────────────────────────────────────────────────────────

    def screenshot_png(self) -> bytes:
        try:
            data = self._tab.Page.captureScreenshot(format="png")
            return base64.b64decode(data["data"])
        except Exception:
            return b""

    def screenshot_gray(self) -> "np.ndarray | None":
        raw = self.screenshot_png()
        if not raw:
            return None
        return cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)

    # ── Cookies via CDP ──────────────────────────────────────────────────────

    def get_cookies_for(self, url: str) -> str:
        """Retorna os cookies do browser para uma URL específica — mesmos que seriam enviados numa requisição real."""
        try:
            result = self._tab.Network.getCookies(urls=[url])
            cookies = result.get("cookies", [])
            return "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        except Exception as e:
            log.warning(f"getCookies falhou para {url}: {e}")
            return ""

    # ── Clique via CDP (sem Runtime) ──────────────────────────────────────────

    def click(self, x: int, y: int) -> None:
        t = self._tab
        try:
            t.Input.dispatchMouseEvent(type="mouseMoved", x=x-2, y=y-2, button="none")
            time.sleep(0.03)
            t.Input.dispatchMouseEvent(type="mouseMoved", x=x, y=y, button="none")
            time.sleep(0.03)
            t.Input.dispatchMouseEvent(type="mousePressed", x=x, y=y, button="left", clickCount=1)
            time.sleep(0.08)
            t.Input.dispatchMouseEvent(type="mouseReleased", x=x, y=y, button="left", clickCount=1)
        except Exception:
            pass

    # ── Espera ativa com template matching ────────────────────────────────────

    def wait_and_click(self, template_name: str, threshold=0.85,
                       timeout=20, click=True) -> bool:
        templates = _load_templates(template_name)
        if not templates:
            log.warning(f"Template nao encontrado: {template_name}.png")
            return False

        deadline = time.monotonic() + timeout
        best = 0.0
        while time.monotonic() < deadline:
            gray = self.screenshot_gray()
            if gray is not None:
                val, cx, cy = _match(gray, templates, threshold)
                if val > best:
                    best = val
                if val >= threshold:
                    log.info(f"OK '{template_name}' conf={val:.2f} -> ({cx},{cy})")
                    if click:
                        self.click(cx, cy)
                    return True
            time.sleep(1.0)

        log.warning(f"FAIL '{template_name}' nao encontrado apos {timeout}s (melhor={best:.2f})")
        # Salva screenshot de diagnóstico para facilitar atualização do template
        try:
            raw = self.screenshot_png()
            if raw:
                import datetime
                out_dir = Path("screenshots")
                out_dir.mkdir(exist_ok=True)
                ts = datetime.datetime.now().strftime("%H%M%S")
                fname = out_dir / f"fail_{template_name}_{ts}.png"
                fname.write_bytes(raw)
                log.info(f"Screenshot de falha salvo: {fname}")
        except Exception:
            pass
        return False

    # ── Detecção de loading ────────────────────────────────────────────────────

    def wait_for_game_load(self, timeout=90) -> bool:
        """Aguarda canvas renderizar OU token ser capturado — o que vier primeiro."""
        import datetime
        deadline = time.monotonic() + timeout
        check_count = 0
        last_click = 0.0
        while time.monotonic() < deadline:
            time.sleep(3)
            check_count += 1

            # Token capturado durante loading = jogo inicializado o suficiente
            if self.token:
                log.info(f"Token capturado durante loading — prosseguindo")
                self._run_bypass_now()
                return True

            shot = self.screenshot_png()
            if shot:
                # Salva screenshot de diagnóstico na pasta screenshots
                try:
                    out_dir = Path("screenshots")
                    out_dir.mkdir(exist_ok=True)
                    ts = datetime.datetime.now().strftime("%H%M%S")
                    with open(out_dir / f"cdp_loading_check_{check_count}_{ts}.png", "wb") as f:
                        f.write(shot)
                except Exception as e:
                    log.error(f"Erro ao salvar diag screenshot: {e}")

                arr = cv2.imdecode(np.frombuffer(shot, np.uint8), cv2.IMREAD_COLOR)
                if arr is not None:
                    ratio = int(np.sum(np.any(arr > 30, axis=2))) / (arr.shape[0]*arr.shape[1])
                    log.debug(f"Loading check: {ratio:.1%} pixels visíveis")
                    if ratio > 0.10:
                        self._run_bypass_now()
                        return True
                    # Clica no centro a cada 20s para destravar loading screen
                    now = time.monotonic()
                    if now - last_click > 20:
                        h, w = arr.shape[:2]
                        self.click(w // 2, h // 2)
                        last_click = now
        return False

    # ── Fluxo visual do jogo ──────────────────────────────────────────────────

    def run_visual_flow(self) -> bool:
        """CONTINUAR -> JOGAR. Etapas posteriores desativadas."""
        log.info("Aguardando 5s para garantir captura do Token...")
        time.sleep(5)

        log.info("Fluxo visual mockado concluído")
        return True

        # ── Etapas desativadas ────────────────────────────────────────────────
        # log.info("Aguardando 8s -> fechando evento...")
        # time.sleep(8)
        # from config import CALIB_CLOSE_EVENT
        # self.click(*CALIB_CLOSE_EVENT)
        # time.sleep(1)
        #
        # log.info("Buscando engrenagem...")
        # for _ in range(3):
        #     if not self.wait_and_click("gear", threshold=0.85, timeout=10):
        #         log.warning("FAIL Engrenagem nao encontrada")
        #         return False
        #     time.sleep(0.5)
        #     if self.wait_and_click("presente", threshold=0.85, timeout=5):
        #         log.info("OK Presente clicado")
        #         break
        #     log.info("Menu fechou - reabrindo...")
        # else:
        #     return False
        #
        # time.sleep(2)
        # log.info("OK Caixa de presente aberta!")
        # return True

    def navigate_to(self, url: str) -> None:
        try:
            self._tab.Page.navigate(url=url)
            log.info("[pychrome] Saiu do jogo — tab na gamelist, WebGL inativo")
        except Exception as exc:
            log.warning(f"[pychrome] navigate_to falhou: {exc}")

    def throttle_rendering(self) -> None:
        tab = self._tab
        js = _RAF_THROTTLE_JS

        def _do():
            ctx_ids: list = []

            def _on_ctx(**kw):
                ctx = kw.get("context", {})
                aux = ctx.get("auxData", {})
                if aux.get("isDefault") or aux.get("type") == "default":
                    cid = ctx.get("id")
                    if cid:
                        ctx_ids.append(cid)

            try:
                tab.Runtime.executionContextCreated = _on_ctx
                tab.Runtime.enable()
                time.sleep(1.0)
                try:
                    tab.Runtime.disable()
                except Exception:
                    pass
                tab.Runtime.executionContextCreated = None
            except Exception as e:
                log.warning(f"[throttle] contextos não enumerados: {e}")

            targets = ctx_ids if ctx_ids else [None]
            ok = 0
            for cid in targets:
                kw = {"expression": js}
                if cid is not None:
                    kw["contextId"] = cid
                try:
                    tab.Runtime.evaluate(**kw)
                    ok += 1
                except Exception:
                    pass

            log.info(f"[throttle] RAF limitado a 1fps em {ok}/{len(targets)} frame(s)")

        threading.Thread(target=_do, daemon=True).start()

    def reload_game(self, url: str, timeout: int = 30) -> bool:
        """Navega para nova URL do jogo e aguarda novo Token ser capturado."""
        self.token = None
        self.api_url = None
        try:
            self._tab.Page.navigate(url=url)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.token:
                    log.info(f"[pychrome] Novo Token após reload: {self.token[:12]}...")
                    return True
                time.sleep(0.5)
            log.warning("[pychrome] Timeout aguardando novo Token após reload")
            return False
        except Exception as exc:
            log.error(f"[pychrome] reload_game falhou: {exc}")
            return False

    def reapply_throttle_sync(self) -> bool:
        """Injeta RAF throttle e verifica se foi aplicado. Retorna True se OK."""
        try:
            self._tab.Runtime.evaluate(expression=_RAF_REAPPLY_JS)
            result = self._tab.Runtime.evaluate(expression="!!window._rafThrottled")
            return result.get("result", {}).get("value", False) is True
        except Exception as e:
            log.warning(f"[throttle] reapply_sync falhou: {e}")
            return False

    def reconnect(self) -> bool:
        """Reconecta pychrome após renderer process restart (novo PID)."""
        if not self._tab_id:
            log.warning("[reconnect] tab_id não armazenado — impossível reconectar")
            return False
        try:
            try:
                self._tab.stop()
            except Exception:
                pass
            browser = pychrome.Browser(url=f"http://127.0.0.1:{self._port}")
            tab = None
            for t in browser.list_tab():
                if t.id == self._tab_id:
                    tab = t
                    break
            if not tab:
                log.warning(f"[reconnect] tab {self._tab_id[:8]}... não encontrada")
                return False
            self._tab = tab
            self._tab.start()
            self._tab.Page.enable()
            self._tab.DOM.enable()
            self._tab.Page.addScriptToEvaluateOnNewDocument(source=STEALTH_JS)
            self._tab.Page.addScriptToEvaluateOnNewDocument(source=_RAF_THROTTLE_JS)
            self._tab.Network.enable()
            self._tab.Network.requestWillBeSent         = self._on_request_will_be_sent
            self._tab.Network.requestWillBeSentExtraInfo = self._on_request_extra_info
            log.info(f"[reconnect] Pychrome reconectado à aba {self._tab_id[:8]}...")
            return True
        except Exception as e:
            log.warning(f"[reconnect] falhou: {e}")
            return False

    def gc(self) -> None:
        """Força garbage collection V8 via CDP."""
        try:
            self._tab.Runtime.collectGarbage()
        except Exception:
            pass

    def stop(self):
        try:
            self._tab.stop()
        except Exception:
            pass


# ── Factory ───────────────────────────────────────────────────────────────────

def take_over_tab(port: int, tab_id: str, bypass_js: str = "") -> "GameTab | None":
    """Conecta pychrome a uma aba existente e aplica stealth sem dar reload."""
    try:
        import pychrome
        browser = pychrome.Browser(url=f"http://127.0.0.1:{port}")
        
        tab = None
        for t in browser.list_tab():
            if t.id == tab_id:
                tab = t
                break
                
        if not tab:
            log.error(f"Aba {tab_id} não encontrada no pychrome")
            return None

        tab.start()
        tab.Page.enable()
        tab.DOM.enable()
        
        # Injeta stealth + throttle em futuros documentos (inclui reload_game)
        tab.Page.addScriptToEvaluateOnNewDocument(source=STEALTH_JS)
        tab.Page.addScriptToEvaluateOnNewDocument(source=_RAF_THROTTLE_JS)

        log.info("pychrome acoplado à aba com sucesso (stealth + bypass ativados)")
        return GameTab(tab, port, bypass_js, tab_id=tab_id)

    except Exception as exc:
        log.error(f"Erro ao assumir aba via pychrome: {exc}")
        return None

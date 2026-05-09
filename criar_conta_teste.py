# SCRIPT CONFIGURÁVEL - SELETORES EM JSON
# FOCO: MANUTENÇÃO FÁCIL E ALTA ESTABILIDADE

import time
import json
import datetime
import os
import shutil
import re
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.common.exceptions import NoSuchWindowException, TimeoutException, WebDriverException
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor
import win32gui
import win32con
import cv2
import numpy as np
import socket
import requests as _req
import io

# =================================================================
#  LISTA DE PROXIES E BLOQUEIOS
# =================================================================

LOCK_ACCOUNTS = threading.Lock()
LOCK_SUCCESS = threading.Lock()

# Set de emails JA RECLAMADOS por threads em execucao (em memoria)
# Evita que duas threads peguem a mesma conta, sem remover do JSON antes do sucesso
_emails_em_progresso: set = set()
_lock_em_progresso = threading.Lock()

# Arquivo para salvar contas com saldo verificado
FILE_SALDO = 'contas_com_saldo.json'
_lock_json_saldo = threading.Lock()


# =================================================================
#  CARREGAMENTO DE CONFIGURACAO (config.json)
# =================================================================
def load_config():
    if not os.path.exists('config.json'):
        log("ERRO: config.json nao encontrado!", "FALHA")
        return None
    with open('config.json', 'r', encoding='utf-8') as f:
        return json.load(f)

CONFIG_DATA = load_config()
if not CONFIG_DATA: exit()

# Atribuição de Configurações
SETTINGS = CONFIG_DATA.get("config", {})
URLS = CONFIG_DATA.get("urls", {})
SEL = CONFIG_DATA.get("selectors", {})

QUANTIDADE_DE_CONTAS = SETTINGS.get("QUANTIDADE_DE_CONTAS", 100)
FILE_ACCOUNTS = 'accounts_teste.json'
FILE_SUCCESS = 'accounts_criadas.json'
STOP_FILE = 'STOP.txt'

# --- CONFIGURAÇÃO DE CALIBRAÇÃO ---
MODO_CALIBRACAO = False
ARQUIVO_COORDENADAS = "coordenadas_jogo.json"
ACAO_CALIBRACAO = ["Continuar", "Jogar", "Fechar evento", "Modo Turbo", "Engrenagem", "Presente"]
CODIGO_BONUS_CALIB = "fogo7779B"
CODIGO_BONUS_CALIB_2 = "fogo777CK"

# =================================================================
#  CONFIGURAÇÃO DE PROXY (Webshare)
# =================================================================
WEBSHARE_API_KEY = "1wlrk9grvep76j9rnjx396u3krcayt7hwpzuz07q"
CHROMIUM_PATH = r"C:\Program Files\Chromium\Application\chrome.exe"
FILE_PROXIES_HISTORICO = "proxies_historico.json"

_proxies_lista = []
_proxy_global_index = 0
_lock_proxy_global = threading.Lock()
_time_ultima_rotacao_proxy = 0.0


def _carregar_historico_ips() -> set:
    try:
        if os.path.exists(FILE_PROXIES_HISTORICO):
            with open(FILE_PROXIES_HISTORICO, "r", encoding="utf-8") as f:
                return set(json.load(f).get("ips_usados", []))
    except Exception:
        pass
    return set()


def _salvar_historico_ips(novos_ips: list):
    historico = _carregar_historico_ips()
    historico.update(novos_ips)
    with open(FILE_PROXIES_HISTORICO, "w", encoding="utf-8") as f:
        json.dump({"ips_usados": sorted(historico)}, f, indent=2)
    log(f"Historico de IPs atualizado: {len(historico)} IPs registrados no total.", "PROXY")


def _criar_extensao_proxy(host, port, user, password):
    manifest = (
        '{"version":"1.0.0","manifest_version":3,"name":"Proxy Auth",'
        '"permissions":["proxy","webRequest","webRequestAuthProvider"],'
        '"host_permissions":["<all_urls>"],'
        '"background":{"service_worker":"background.js"}}'
    )
    background = (
        f'chrome.proxy.settings.set({{value:{{mode:"fixed_servers",'
        f'rules:{{singleProxy:{{scheme:"http",host:"{host}",port:parseInt({port})}},'
        f'bypassList:["localhost"]}}}},scope:"regular"}},function(){{}});\n'
        f'chrome.webRequest.onAuthRequired.addListener(\n'
        f'  function(details){{return{{authCredentials:{{username:"{user}",password:"{password}"}}}}}},\n'
        f'  {{urls:["<all_urls>"]}},["blocking"]\n'
        f');'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('manifest.json', manifest)
        zf.writestr('background.js', background)
    return buf.getvalue()


def carregar_proxies():
    global _proxies_lista, _proxy_global_index
    log("Carregando proxies da Webshare API...", "PROXY")
    try:
        headers = {"Authorization": f"Token {WEBSHARE_API_KEY}"}
        r = _req.get(
            "https://proxy.webshare.io/api/v2/proxy/list/",
            headers=headers,
            params={"mode": "direct", "page_size": 100, "valid": "true"},
            timeout=15
        )
        if r.status_code != 200:
            log(f"Erro Webshare: HTTP {r.status_code}", "FALHA")
            return False
        results = r.json().get("results", [])
        if not results:
            log("Nenhuma proxy disponivel na conta Webshare.", "AVISO")
            return False
        _proxies_lista = [
            f"{p['proxy_address']}:{p['port']}:{p['username']}:{p['password']}"
            for p in results if p.get('proxy_address') and p.get('port')
        ]
        _proxy_global_index = 0
        # Registra os IPs atuais no historico permanente
        ips_atuais = [p.split(":")[0] for p in _proxies_lista]
        _salvar_historico_ips(ips_atuais)
        log(f"{len(_proxies_lista)} proxies carregadas com sucesso!", "PROXY")
        return True
    except Exception as e:
        log(f"Erro ao conectar Webshare API: {e}", "FALHA")
        return False


def _executar_replace_webshare(hdrs, ips_para_substituir):
    """Envia requisicao de replace e aguarda conclusao. Retorna True se concluiu."""
    body = {
        "dry_run": False,
        "to_replace": {"type": "ip_address", "ip_addresses": ips_para_substituir},
        "replace_with": [{"type": "country", "country_code": "US", "count": len(ips_para_substituir)}],
    }
    r2 = _req.post("https://proxy.webshare.io/api/v3/proxy/replace/", headers=hdrs, json=body, timeout=15)
    if r2.status_code not in (200, 201):
        log(f"Erro no replace: {r2.status_code} — {r2.text[:100]}", "FALHA")
        return False
    rid = r2.json().get("id")
    log(f"Rotacao iniciada (ID={rid}). Aguardando conclusao...", "PROXY")
    for _ in range(18):
        time.sleep(5)
        st = _req.get(f"https://proxy.webshare.io/api/v3/proxy/replace/{rid}/", headers=hdrs, timeout=10)
        if st.status_code == 200 and st.json().get("state") == "completed":
            log("Rotacao concluida!", "PROXY")
            return True
    log("Timeout na rotacao.", "AVISO")
    return False


def rotacionar_proxies():
    log("Rotacionando todos os IPs via Webshare API...", "PROXY")
    try:
        hdrs = {"Authorization": f"Token {WEBSHARE_API_KEY}", "Content-Type": "application/json"}

        MAX_TENTATIVAS = 3
        for tentativa in range(MAX_TENTATIVAS):
            # Snapshot do historico ANTES desta rotacao
            historico_antes = _carregar_historico_ips()

            # Pega IPs atualmente atribuidos
            r = _req.get("https://proxy.webshare.io/api/v2/proxy/list/",
                         headers=hdrs, params={"mode": "direct", "page_size": 100}, timeout=15)
            if r.status_code != 200:
                break
            atuais = r.json().get("results", [])
            if not atuais:
                break
            ips_atuais = [p["proxy_address"] for p in atuais]

            ok = _executar_replace_webshare(hdrs, ips_atuais)
            if not ok:
                break

            # Carrega os novos IPs (tambem os registra no historico)
            time.sleep(3)
            carregar_proxies()
            novos_ips = [p.split(":")[0] for p in _proxies_lista]

            # Verifica quais novos IPs ja estavam no historico antes desta rotacao
            repetidos = set(novos_ips) & historico_antes
            if not repetidos:
                log(f"Todos os {len(novos_ips)} IPs sao ineditos. Historico total: {len(historico_antes) + len(novos_ips)} IPs.", "PROXY")
                return True

            log(f"Tentativa {tentativa+1}/{MAX_TENTATIVAS}: {len(repetidos)} IP(s) repetido(s) no historico. Rotacionando novamente...", "PROXY")

        log("Rotacao concluida (limite de tentativas atingido).", "PROXY")
        return True
    except Exception as e:
        log(f"Erro ao rotacionar proxies: {e}", "FALHA")
        return carregar_proxies()


def obter_proxy():
    global _proxy_global_index
    with _lock_proxy_global:
        if not _proxies_lista:
            return None
        proxy = _proxies_lista[_proxy_global_index % len(_proxies_lista)]
        _proxy_global_index = (_proxy_global_index + 1) % len(_proxies_lista)
        return proxy


def log(msg, tags="OK"):
    timestamp = datetime.datetime.now().strftime('%H:%M:%S')
    print(f"[{timestamp}] [{tags}] {msg}", flush=True)

def forcar_foco_pela_janela(driver):
    """Usa Win32 API para trazer a janela do driver para frente de TUDO."""
    try:
        # Tenta pelo título da janela atual do Selenium
        title = driver.title
        if not title: return False
        
        hwnd = win32gui.FindWindow(None, title)
        if hwnd:
            # Força a janela para frente (Restore + SetForeground + Top)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            win32gui.BringWindowToTop(hwnd)
            return True
    except:
        pass
    return False

# Cache do STOP_FILE: Verificado no maximo uma vez por segundo
_stop_last_check = 0.0
_stop_cached = False

def check_stop():
    global _stop_last_check, _stop_cached
    now = time.time()
    if now - _stop_last_check > 1.0:
        _stop_cached = os.path.exists(STOP_FILE)
        _stop_last_check = now
    return _stop_cached

def get_sucessos_anteriores():
    """Retorna sets com emails e perfis ja criados (rapido para lookup)."""
    if not os.path.exists(FILE_SUCCESS): return set(), set()
    try:
        with open(FILE_SUCCESS, 'r', encoding='utf-8') as f:
            data = json.load(f)
        criadas = data.get('contas_criadas', data.get('contas', []))
        emails = {c.get('email', '').lower() for c in criadas if isinstance(c, dict)}
        perfis = {c.get('perfil', '') for c in criadas if isinstance(c, dict)}
        return emails, perfis
    except: return set(), set()

def carregar_conta():
    """Marca e retorna a proxima conta disponivel (NAO remove do JSON ainda)."""
    with LOCK_ACCOUNTS:
        if not os.path.exists(FILE_ACCOUNTS): return None, None, None
        try:
            with open(FILE_ACCOUNTS, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            contas = data.get('contas_teste', [])
            if not contas: 
                log("AVISO: Nenhuma conta encontrada em accounts_teste.json", "INFO")
                return None, None, None
            
            emails_criados, perfis_criados = get_sucessos_anteriores()
            
            # Busca a primeira conta que nao foi criada e nao esta em progresso
            with _lock_em_progresso:
                for conta in contas:
                    email = conta.get('email', '').lower()
                    perfil = conta.get('perfil', '')
                    
                    # PULA SE: JA CRIADO (EMAIL), OU JA CRIADO (PERFIL), OU EM PROGRESSO
                    if email in emails_criados or perfil in perfis_criados or email in _emails_em_progresso:
                        continue
                        
                    _emails_em_progresso.add(email)
                    return email, conta.get('password', ''), perfil
        except Exception as e:
            log(f"Erro ao carregar conta: {e}", "FALHA")
        return None, None, None

def liberar_conta_falha(email):
    """Remove um email do set de em_progresso se a thread falhar.
    Isso permite que a conta seja tentada novamente na proxima execucao do script."""
    if email:
        with _lock_em_progresso:
            _emails_em_progresso.discard(email.lower())

def salvar_sucesso(email, password, perfil):
    """Salva o sucesso E remove a conta de accounts_teste.json (unico ponto de remocao)."""
    # 1. Remove do set em_progresso
    with _lock_em_progresso:
        _emails_em_progresso.discard(email.lower())
    
    # 2. Remove de accounts_teste.json
    with LOCK_ACCOUNTS:
        try:
            if os.path.exists(FILE_ACCOUNTS):
                with open(FILE_ACCOUNTS, 'r', encoding='utf-8') as f:
                    data_teste = json.load(f)
                contas_restantes = [c for c in data_teste.get('contas_teste', []) 
                                    if c.get('email', '').lower() != email.lower()]
                data_teste['contas_teste'] = contas_restantes
                with open(FILE_ACCOUNTS, 'w', encoding='utf-8') as f:
                    json.dump(data_teste, f, indent=4)
        except Exception as e:
            log(f"Aviso: nao removeu de accounts_teste: {e}", "DIAG")
    
    # 3. Adiciona a accounts_criadas.json
    with LOCK_SUCCESS:
        data = {"contas_criadas": []}
        if os.path.exists(FILE_SUCCESS):
            try:
                with open(FILE_SUCCESS, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except: pass
        data["contas_criadas"].append({
            "email": email, 
            "password": password, 
            "perfil": perfil, 
            "timestamp": datetime.datetime.now().isoformat()
        })
        with open(FILE_SUCCESS, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4)


# =================================================================
#  INTERACAO ROBUSTA (LEITURA DINAMICA DO JSON)
# =================================================================
def verificar_popup_bonus(driver):
    try:
        # Seletor definido no config.json como "popup_bonus_yes"
        # Usando a função interna para não causar loop infinito se chamasse 'clicar'
        selector_list = SEL.get("popup_bonus_yes", [])
        for by_str, valor in selector_list:
            try:
                elements = driver.find_elements(by_str, valor)
                for el in elements:
                    if el.is_displayed():
                        log("Popup Bonus detectado! Clicando em YES...", "POPUP")
                        driver.execute_script("arguments[0].click();", el)
                        return True
            except: pass
    except: pass
    return False

def verificar_checkface(driver):
    """Monitora a URL por redirects indesejados para checkFace e tenta voltar."""
    try:
        if 'checkFace' in driver.current_url:
            log("CheckFace detectado! Restaurando resolucao para 480x800 e executando driver.back()...", "AVISO")
            try: driver.set_window_size(480, 800)
            except: pass
            driver.back()
            time.sleep(0.6)
            return True
    except: pass
    return False

def fechar_popup_generico(driver):
    """Tenta fechar APENAS alerts JS nativos e janelas extras nao-Google.
    NAO tenta fechar elementos da pagina para evitar interferir no fluxo."""
    try:
        # Aceita alert nativo do browser (dialogs JS)
        try:
            alert = driver.switch_to.alert
            alert.accept()
            log("Dialog nativo do browser fechado.", "AVISO")
            return True
        except: pass
        
        # Tenta fechar janelas extras (pop-ups novos que nao sejam o popup do Google)
        main = driver.current_window_handle
        for handle in driver.window_handles:
            if handle != main:
                try:
                    driver.switch_to.window(handle)
                    url = driver.current_url
                    # So fecha se nao for o popup do Google em uso
                    if "accounts.google.com" not in url and "google.com" not in url:
                        driver.close()
                        log(f"Janela extra fechada: {url[:60]}", "AVISO")
                except: pass
        driver.switch_to.window(main)
    except: pass
    return False

def procurar_em_iframes(driver, selector_list, depth=0):
    if depth > 2: return None
    
    # Busca DIRETA no contexto atual (mais rapido - nao entra em iframe por padrao)
    for by_str, valor in selector_list:
        try:
            elements = driver.find_elements(by_str, valor)
            for el in elements:
                if el.is_displayed(): return el
        except: pass
    
    # Busca em iframes APENAS na profundidade 0 (evita latencia recursiva)
    if depth == 0:
        try:
            iframes = driver.find_elements(By.TAG_NAME, "iframe")
            for iframe in iframes:
                try:
                    driver.switch_to.frame(iframe)
                    found = procurar_em_iframes(driver, selector_list, depth + 1)
                    if found: return found
                    driver.switch_to.parent_frame()
                except: 
                    try: driver.switch_to.default_content()
                    except: pass
                    continue
        except: pass
    return None

def clicar(driver, selector_list, timeout=5, desc="", silencioso=False, use_standard=False, skip_monitors=False):
    if not selector_list: return False
    start = time.time()
    last_monitor_check = 0.0
    while time.time() - start < timeout:
        if check_stop(): return False
        
        # Monitor throttled (a cada 2s), apenas fora do Google login
        now = time.time()
        if not skip_monitors and (now - last_monitor_check > 2.0):
            verificar_popup_bonus(driver)
            verificar_checkface(driver)
            last_monitor_check = time.time()

        try:
            driver.switch_to.default_content()
            el = procurar_em_iframes(driver, selector_list)
            if el:
                if not silencioso:
                    try: 
                        tag = el.tag_name
                        txt = el.text[:20].strip().replace('\n', ' ')
                        log(f"Clicou: {desc} ({tag}:'{txt}')")
                    except: log(f"Clicou: {desc}")
                
                try:
                    if use_standard: el.click()
                    else: driver.execute_script("arguments[0].click();", el)
                except:
                    try:
                        if not use_standard: el.click()
                        else: driver.execute_script("arguments[0].click();", el)
                    except: pass
                
                try: driver.switch_to.default_content()
                except: pass
                return True
        except: pass
        time.sleep(0.15)
    if not silencioso: log(f"Nao achou: {desc}", "AVISO")
    try: driver.switch_to.default_content()
    except: pass
    return False

def digitar(driver, selector_list, texto, timeout=5, desc="", skip_monitors=False):
    if not selector_list: return False
    start = time.time()
    last_monitor_check = 0.0
    while time.time() - start < timeout:
        if check_stop(): return False
        
        now = time.time()
        if not skip_monitors and (now - last_monitor_check > 2.0):
            verificar_popup_bonus(driver)
            verificar_checkface(driver)
            last_monitor_check = time.time()

        try:
            driver.switch_to.default_content()
            el = procurar_em_iframes(driver, selector_list)
            if el:
                el.clear()
                time.sleep(0.05)
                el.send_keys(texto)
                time.sleep(0.1)
                # VALIDACAO: confirma que o texto foi realmente digitado
                try:
                    val = el.get_attribute('value') or ''
                    if texto.lower() not in val.lower() and len(val) < len(texto) // 2:
                        log(f"AVISO: texto nao confirmado em campo '{desc}'. Retentando...", "DIAG")
                        el.clear()
                        time.sleep(0.1)
                        el.send_keys(texto)
                except: pass
                log(f"Digitou: {desc}")
                try: driver.switch_to.default_content()
                except: pass
                return True
        except: pass
        time.sleep(0.15)
    try: driver.switch_to.default_content()
    except: pass
    return False


def limpar_perfil(profile_path):
    log(f"Limpando arquivos desnecessarios em: {os.path.basename(profile_path)}", "LIMPEZA")
    # Pastas pesadas que podem ser apagadas sem perder a sessão
    folders_to_delete = [
        "Cache", "Code Cache", "GPUCache", "ShaderCache", 
        "Service Worker/CacheStorage", "Service Worker/ScriptCache",
        "GrShaderCache", "GraphiteDawnCache", "DawnCache",
        "Media Cache", "WebStorage", "IndexedDB", "Blob Storage",
        "File System", "Session Storage", "Extension State",
        "Extension Rules", "Extension Scripts",
        "BrowserMetrics", "component_crx_cache", "optimization_guide_model_store",
        "WasmTtsEngine", "OnDeviceHeadSuggestModel", "Crashpad", "SafetyTips"
    ]
    files_to_delete = [
        "System Reporting", "TransportSecurity", "History", "Visited Links", 
        "Web Data", "LOG", "LOG.old", "Last Browser", "Last Version"
    ]
    
    # Procura em Default (onde a maioria dos dados reside)
    default_path = os.path.join(profile_path, "Default")
    
    targets = [profile_path]
    if os.path.exists(default_path):
        targets.append(default_path)
    
    for base in targets:
        for folder in folders_to_delete:
            p = os.path.join(base, folder)
            if os.path.exists(p):
                try: shutil.rmtree(p, ignore_errors=True)
                except: pass
        for file in files_to_delete:
            p = os.path.join(base, file)
            if os.path.exists(p):
                try: os.remove(p)
                except: pass

def criar_driver(email, indice, perfil, proxy_str=None):
    profile = os.path.abspath(f"UserData/{perfil}")
    os.makedirs(profile, exist_ok=True)
    
    # 1. LIMPEZA DE TRAVA (SingletonLock)
    lock_file = os.path.join(profile, "SingletonLock")
    if os.path.exists(lock_file):
        try: os.remove(lock_file)
        except: pass
    
    log(f"[{perfil}] Iniciando navegador (Modo Direto)...", "INFO")

    # Posição calculada: 3 colunas de 480px
    x = ((indice - 1) % 3) * 480
    y = 0

    opts = Options()
    for _cb in [
        CHROMIUM_PATH,
        rf"C:\Users\{os.environ.get('USERNAME', '')}\AppData\Local\Chromium\Application\chrome.exe",
        r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
    ]:
        if os.path.exists(_cb):
            opts.binary_location = _cb
            log(f"[{perfil}] Usando Chromium: {_cb}", "INFO")
            break
    opts.add_argument(f"user-data-dir={profile}")
    opts.add_argument(f"--window-size=480,800")
    opts.add_argument(f"--window-position={x},{y}")
    opts.add_argument("--lang=pt-BR")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--mute-audio")
    
    # --- OTIMIZAÇÃO DE ESPAÇO (DISCO) ---
    opts.add_argument("--disk-cache-size=1")
    opts.add_argument("--media-cache-size=1")
    opts.add_argument("--disable-application-cache")
    opts.add_argument("--disable-offline-load-stale-cache")
    opts.add_argument("--memory-cache-size=0")
    opts.add_argument("--disable-gpu-shader-disk-cache")
    opts.add_argument("--disable-features=OptimizationGuideModelDownloading,OptimizationHints,OptimizationTargetPrediction,OptimizationGuide")
    
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("detach", True)

    if proxy_str:
        parts = proxy_str.split(":")
        if len(parts) == 4:
            _h, _p, _u, _pw = parts
            _ext = _criar_extensao_proxy(_h, _p, _u, _pw)
            _ext_path = os.path.join(os.environ.get("TEMP", "."), f"proxy_ext_{indice}.zip")
            with open(_ext_path, "wb") as _ef:
                _ef.write(_ext)
            opts.add_extension(_ext_path)
            log(f"[{perfil}] Proxy configurada: {_h}:{_p}", "PROXY")

    for tentativa in range(3):
        try:
            try:
                from webdriver_manager.core.os_manager import ChromeType
                service = Service(ChromeDriverManager(chrome_type=ChromeType.CHROMIUM).install())
            except Exception:
                service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=opts)
            
            # Truque de Foco: Win32 API para trazer para frente de TUDO
            try:
                time.sleep(0.5) # Espera renderizar o título
                forcar_foco_pela_janela(driver)
            except: pass
            
            driver.get(URLS["login"])
            time.sleep(2)  # Garante tempo de renderização para ambos os browsers carregando em paralelo
            return driver
        except Exception as e:
            err_str = str(e)
            log(f"Erro ao criar driver (Tentativa {tentativa+1}): {err_str[:200]}", "FALHA")
            # Proxy falhou → retorna None imediatamente para o caller tentar outra proxy
            if "ERR_PROXY_CONNECTION_FAILED" in err_str or "ERR_TUNNEL_CONNECTION_FAILED" in err_str or "ERR_SOCKS_CONNECTION_FAILED" in err_str:
                log(f"Proxy morta/inacessivel. Abortando para trocar proxy.", "AVISO")
                return None
            elif "ERR_NAME_NOT_RESOLVED" in err_str or "ERR_NETWORK_CHANGED" in err_str or "ERR_CONNECTION_CLOSED" in err_str:
                log(f"Erro de rede detectado. Aguardando 25s para reestabelecer...", "AVISO")
                time.sleep(25)
            elif "Chrome instance exited" in err_str or "session not created" in err_str:
                log(f"Chrome colapsou (Recursos/Crash). Limpando lixo e aguardando 30s...", "AVISO")
                for folder in ["Crashpad", "ShaderCache", "GraphiteDawnCache"]:
                    p = os.path.join(profile, folder)
                    if os.path.exists(p): shutil.rmtree(p, ignore_errors=True)
                time.sleep(30)
            else:
                time.sleep(15)
    return None


# =================================================================
#  PROCESSO AUTO-REGENERATIVO
# =================================================================
def tratar_popup_google(driver, email, password):
    main_window = driver.current_window_handle
    popup_window = None

    try:
        # Tenta achar popup já aberto
        for handle in driver.window_handles:
            if handle != main_window:
                try:
                    driver.switch_to.window(handle)
                    if "accounts.google.com" in driver.current_url:
                        popup_window = handle
                        log("Detectado popup do Google já aberto.", "DIAG")
                        break
                except: pass
        
        if not popup_window:
            WebDriverWait(driver, 15).until(EC.number_of_windows_to_be(2))
            popup_window = driver.window_handles[-1]
        
        # IMPORTANTE: Pega a posição do navegador PRINCIPAL antes de trocar de janela
        main_rect = driver.get_window_rect()
        
        driver.switch_to.window(popup_window)
        
        # Posiciona e FORÇA FOCO com Win32
        try:
            driver.set_window_rect(main_rect['x'], main_rect['y'], 480, 600)
            time.sleep(0.5)
            forcar_foco_pela_janela(driver)
        except: pass
        
        log(" Popup Aberta. Checando 'Escolha de Conta'...", "DIAG")
        
        # 1. ATALHO: CONTA JÁ LISTADA
        selector_chooser = [
            ["css selector", f"div[data-email='{email}']"],
            ["xpath", f"//div[@data-email='{email}']"],
            ["xpath", f"//div[contains(@jsname, 'bQIQze') and contains(text(), '{email}')]"]
        ]
        
        if clicar(driver, selector_chooser, timeout=8, desc=f"Conta ({email}) Escolha", skip_monitors=True):
            # Validação: Se ainda estiver na tela de escolha após 2s, tenta clique padrão
            time.sleep(2)
            if procurar_em_iframes(driver, selector_chooser):
                log(f"Ainda na tela de escolha para {email}. Tentando clique padrão...", "RETRY")
                clicar(driver, selector_chooser, timeout=5, desc="Clique Padrão Chooser", use_standard=True, skip_monitors=True)
            log(f"Conta {email} selecionada!", "DIAG")
        else:
            # 2. FLUXO NORMAL (Se não achou na lista)
            if digitar(driver, SEL["google_email_input"], email, desc="E-mail", skip_monitors=True):
                clicar(driver, SEL["google_next_button"], desc="Next", use_standard=True, skip_monitors=True)
                time.sleep(1.0) 
                if digitar(driver, SEL["google_password_input"], password, desc="Senha", skip_monitors=True):
                    clicar(driver, SEL["google_password_next"], desc="Confirmar", use_standard=True, skip_monitors=True)
        
        # 3. CONSENTIMENTO (Loop de tentativa para telas lentas)
        for consent_retry in range(5):
            try:
                # Guarda-popup: sai se fechou
                if len(driver.window_handles) < 2:
                    log("Popup fechada antes do consentimento.", "DIAG")
                    break
                # Garante foco no popup antes de cada tentativa
                try:
                    driver.switch_to.window(popup_window)
                except Exception:
                    break
                
                # Seletor "Entendi"
                clicar(driver, SEL["google_entendi_button"], timeout=3, desc="Entendi", silencioso=True, use_standard=True, skip_monitors=True)
                time.sleep(0.5)

                # Seletor "Continuar" (Principal) — tenta JS e clique padrao
                clicou_continuar = clicar(driver, SEL["google_continue_button"], timeout=4, desc="Continuar", silencioso=True, use_standard=True, skip_monitors=True)
                if not clicou_continuar:
                    clicou_continuar = clicar(driver, SEL["google_continue_button"], timeout=3, desc="Continuar-JS", silencioso=True, use_standard=False, skip_monitors=True)
                
                if clicou_continuar:
                    log(f"Sucesso no clique do consentimento (tentativa {consent_retry+1})!", "DIAG")
                    # Valida que o popup comecou a fechar
                    time.sleep(1.0)
                    if len(driver.window_handles) < 2:
                        break
                    # Se ainda aberto, tenta mais uma vez
                
                time.sleep(1.5)
            except Exception as e_consent:
                log(f"Excecao no consentimento retentativa {consent_retry+1}: {e_consent}", "DIAG")
                break
        
        try:
            # Pós-clique: aguarda estabilização antes da Dead Zone
            time.sleep(1)
            dead_sleep = min(SETTINGS.get('DEAD_ZONE_SLEEP', 5), 3)
            log(f"Entrando em DEAD ZONE de {dead_sleep}s...", "DIAG")
            time.sleep(dead_sleep)
        except: pass
        
        # FORÇAR FOCO NO POPUP: Maximizar e Restaurar
        try:
            driver.maximize_window()
            time.sleep(0.3)
            driver.set_window_rect(100, 100, 520, 650)
        except: pass
        
        log(f"[{email}] Popup focada. Iniciando preenchimento...", "DIAG")

        # MONITORAMENTO PASSIVO
        p_start = time.time()
        while time.time() - p_start < SETTINGS.get("PASSIVE_WAIT_TIMEOUT", 30):
            if len(driver.window_handles) < 2:
                log("Popup fechada sozinha!", "DIAG")
                break
            time.sleep(2)
            
    except Exception as e:
        log(f"Aviso Popup: {e}", "DIAG")

def buscar_jogo_scroll(driver):
    log("Iniciando busca do jogo com Scroll e Load More...", "GAME")
    
    # LOOP DE SEGURANÇA NO SCROLL: Padrão original estável
    for step in range(3):
        # Verifica checkFace ANTES de scrollar
        try:
            url_scroll = driver.current_url
            if 'checkFace' in url_scroll:
                log("CheckFace detectado no lobby. Restaurando resolucao 480x800 e Recarregando...", "AVISO")
                try: driver.set_window_size(480, 800)
                except: pass
                driver.get(URLS['gameList'])
                time.sleep(1.5)
        except: pass

        if step == 0:
            # Passo A: Scroll e "Carregar Mais" (Nível 1)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.2)
            if clicar(driver, SEL["load_more_1"], timeout=2.5, desc="Load More 1", silencioso=True):
                time.sleep(0.2) 
        
        elif step == 1:
            # Passo B: Scroll e "Carregar Mais" (Nível 2)
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.15)
            if clicar(driver, SEL["load_more_2"], timeout=2.0, desc="Load More 2", silencioso=True):
                time.sleep(0.2) 

        elif step == 1:
            # Passo C: Localização do Jogo Final
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(0.1)
            
        # --- REFORÇO DE SEGURANÇA ---
        if procurar_em_iframes(driver, SEL.get("login_button_check", [])):
            log("SESSAO CAIU DURANTE SCROLL!", "ERRO")
            return "RELOGIN"
    
    # Tenta encontrar o jogo APÓS os scrolls conforme o padrão original
    for tentativa_jogo in range(3):
        el = procurar_em_iframes(driver, SEL["target_game"])
        if el:
            log(f"JOGO ENCONTRADO (tentativa {tentativa_jogo+1})! Rolando até ele...", "SUCESSO")
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", el)
            time.sleep(0.1) 
            try:
                el.click()
            except:
                driver.execute_script("arguments[0].click();", el)
            return True
        
        # Nao achou: scroll pequeno e re-tenta
        if tentativa_jogo < 2:
            driver.execute_script("window.scrollBy(0, 500);")
            time.sleep(0.2)
    
    return False

def tratar_relogin_google(driver, email, password):
    log("Tentando RE-LOGIN (Conta Salva)...", "RETRY")
    
    # Guarda janela principal
    main_window = driver.current_window_handle
    popup_window = None

    # 1. Tenta achar popup já aberto (evita duplicidade)
    for handle in driver.window_handles:
        if handle != main_window:
            try:
                driver.switch_to.window(handle)
                if "accounts.google.com" in driver.current_url:
                    popup_window = handle
                    log("Reusando popup do Google já aberto.", "DIAG")
                    break
            except: pass
    
    if not popup_window:
        try:
            # Tenta aguardar popup por ate 5 segundos
            WebDriverWait(driver, 5).until(EC.number_of_windows_to_be(2))
            for handle in driver.window_handles:
                if handle != main_window:
                    popup_window = handle
                    break
        except:
            log("Nenhum popup adicional detectado. Prosseguindo na mesma aba.", "DIAG")
    
    if popup_window:
        # Se tem popup, posiciona e foca nele
        try:
            main_rect = driver.get_window_rect()
            driver.switch_to.window(popup_window)
            
            # FORÇAR FOCO NO RELOGIN: Win32
            try:
                driver.set_window_rect(main_rect['x'], main_rect['y'], 480, 600)
                time.sleep(0.5)
                forcar_foco_pela_janela(driver)
            except: pass
            
            time.sleep(1) # Estabiliza popup
        except: pass
    
    try:
        # Seleciona a conta específica (Chooser)
        conta_especifica = [
            ["css selector", f"div[data-email='{email}']"],
            ["xpath", f"//div[@data-email='{email}']"],
            ["xpath", f"//div[contains(@jsname, 'bQIQze') and contains(text(), '{email}')]"]
        ]
        
        if clicar(driver, conta_especifica, timeout=8, desc=f"Conta Re-Login ({email})", skip_monitors=True):
            # Validação: Se ainda estiver na tela de escolha após 2s, tenta clique padrão
            time.sleep(2)
            if procurar_em_iframes(driver, conta_especifica):
                log(f"Ainda na tela de Re-Login para {email}. Tentando clique padrão...", "RETRY")
                clicar(driver, conta_especifica, timeout=5, desc="Clique Padrão Re-Login", use_standard=True, skip_monitors=True)
        elif clicar(driver, SEL["google_account_chooser_first"], timeout=5, desc="Conta Salva (Genérica)", skip_monitors=True):
            pass
        else:
            # Se não achou lista, talvez tenha caído direto no login normal
            if digitar(driver, SEL["google_email_input"], email, desc="E-mail (Re-Login)", skip_monitors=True):
                clicar(driver, SEL["google_next_button"], desc="Next", use_standard=True, skip_monitors=True)
        
        time.sleep(2)
        
        # Se pedir senha novamente (pode acontecer no re-login)
        if digitar(driver, SEL["google_password_input"], password, desc="Senha (Re-Login)", skip_monitors=True):
             clicar(driver, SEL["google_password_next"], desc="Confirmar", use_standard=True, skip_monitors=True)

        
        # Confirmação Intermediária (se houver)
        for consent_retry in range(3):
            try:
                if len(driver.window_handles) < 2: break
                driver.switch_to.window(popup_window)
                
                # Tenta Interstitial
                clicar(driver, SEL["google_continue_interstitial"], timeout=3, desc="Continuar Interstitial", silencioso=True, use_standard=True, skip_monitors=True)
                
                # Tenta Final
                if clicar(driver, SEL["google_continue_button"], timeout=3, desc="Continuar Final", silencioso=True, use_standard=True, skip_monitors=True):
                    log("Sucesso no clique do re-login!", "DIAG")
                    break
                
                time.sleep(1.5)
            except: break
        
        # Aguarda fechar popup com timeout maior
        p_start = time.time()
        while time.time() - p_start < 30:
            if len(driver.window_handles) < 2: break
            time.sleep(1)
            
    except Exception as e:
        log(f"Erro no Re-Login: {e}", "FALHA")

# =================================================================
#  HANDOFF: FECHA O PROXY E ABRE NAVEGADOR LIMPO NO JOGO
# =================================================================
def handoff_sem_proxy(driver_proxy, indice):
    """
    Apos o jogo carregar no navegador com proxy:
    1. Coleta os cookies da sessao
    2. Abre um NOVO navegador sem proxy
    3. Injeta os cookies e navega para o jogo
    4. Fecha o navegador com proxy (economia de dados)
    Retorna o novo driver sem proxy, ou None se falhar.
    """
    log(f"[{indice}] Iniciando handoff: coletando sessao do proxy...", "PROXY")
    play_url = driver_proxy.current_url
    
    try:
        # 1. Coleta todos os cookies da sessao atual
        cookies = driver_proxy.get_cookies()
        site_url = "/".join(play_url.split("/")[:3])  # Ex: https://exemplo.com
        
        # 2. Abre um novo navegador SEM proxy
        profile_clean = os.path.abspath(f"ChromeProfiles/P_{indice}_play")
        if os.path.exists(profile_clean):
            shutil.rmtree(profile_clean, ignore_errors=True)
        os.makedirs(profile_clean, exist_ok=True)
        
        opts_clean = Options()
        opts_clean.add_argument(f"user-data-dir={profile_clean}")
        opts_clean.add_argument("--lang=pt-BR")
        opts_clean.add_argument("--no-sandbox")
        opts_clean.add_argument("--disable-dev-shm-usage")
        opts_clean.add_argument("--remote-allow-origins=*")
        opts_clean.add_argument("--enable-gpu")
        opts_clean.add_argument("--ignore-gpu-blocklist")
        opts_clean.add_argument("--enable-webgl")
        opts_clean.add_argument("--use-gl=desktop")
        opts_clean.add_argument("--no-first-run")
        opts_clean.add_argument("--no-proxy-server")  # Garante sem proxy
        opts_clean.add_argument("--disable-blink-features=AutomationControlled")
        opts_clean.add_argument("--disable-web-security")  # Evita ERR_NETWORK_ACCESS_DENIED
        opts_clean.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts_clean.add_experimental_option("useAutomationExtension", False)
        opts_clean.add_experimental_option("detach", True)
        opts_clean.add_argument("--disk-cache-size=1")
        
        driver_clean = webdriver.Chrome(service=Service(), options=opts_clean)
        
        # Posiciona no mesmo lugar do navegador proxy
        x = (indice % 2) * 500 + (indice // 2) * 15
        driver_clean.set_window_rect(x, 0, 480, 800)
        
        # AGUARDA O CHROME INICIALIZAR COMPLETAMENTE antes de navegar
        time.sleep(3)
        
        # 3. Navega para o site para poder injetar os cookies
        driver_clean.get(site_url)
        time.sleep(2)
        
        for cookie in cookies:
            try:
                # Remove campos que podem causar erro ao injetar
                cookie.pop('sameSite', None)
                cookie.pop('expiry', None)
                driver_clean.add_cookie(cookie)
            except: pass
        
        # 4. Navega para a URL do jogo com a sessao injetada
        driver_clean.get(play_url)
        time.sleep(2)
        
        # 5. Fecha o navegador com proxy
        try:
            driver_proxy.quit()
        except: pass
        
        log(f"[{indice}] Handoff concluido! Proxy desligada, jogo aberto sem proxy.", "SUCESSO")
        return driver_clean
        
    except Exception as e:
        log(f"[{indice}] Erro no handoff: {e}. Mantendo navegador original.", "AVISO")
        return None

# =================================================================
#  FUNÇÕES DE CALIBRAÇÃO (INTEGRADO)
# =================================================================

def calib_redimensionar(driver):
    try:
        # driver.set_window_size(384, 258)
        log("Mantendo resolução 480x800 para calibração/Visão Computacional.", "CALIB")
    except Exception as e:
        log(f"Aviso ao manter resolução: {e}", "CALIB")

def verificar_saldo(driver):
    """Navega ao /play e extrai o saldo do div.user. Retorna string ex: '0.40'."""
    import re
    try:
        driver.switch_to.default_content()
        wait = WebDriverWait(driver, 10)
        # Tenta primeiro pelo seletor do cabeçalho .user
        try:
            el = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, ".user")))
            texto = el.text.strip()
            match = re.search(r'[\d]+[,\.]?[\d]*', texto)
            if match:
                saldo = match.group().replace(',', '.')
                log(f"Saldo verificado: R$ {saldo}", "SALDO")
                return saldo
        except: pass
    except Exception as e:
        log(f"Erro ao verificar saldo: {e}", "FALHA")
    return "0.00"

def salvar_conta_com_saldo(email, password, perfil, saldo):
    """Salva conta com saldo verificado em contas_com_saldo.json (thread-safe)."""
    import datetime as _dt
    with _lock_json_saldo:
        dados = []
        if os.path.exists(FILE_SALDO):
            try:
                with open(FILE_SALDO, 'r', encoding='utf-8') as f:
                    dados = json.load(f)
            except: dados = []
        
        # O arquivo pode ser uma lista pura [] ou um dicionário {"contas_com_saldo": []} gerado pela GUI
        novo_registro = {
            "email": email,
            "password": password,
            "perfil": perfil,
            "saldo": saldo,
            "timestamp": _dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        if isinstance(dados, dict) and "contas_com_saldo" in dados:
            dados["contas_com_saldo"].append(novo_registro)
        elif isinstance(dados, list):
            dados.append(novo_registro)
        else:
            dados = [novo_registro]
            
        with open(FILE_SALDO, 'w', encoding='utf-8') as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
        log(f"Conta salva em {FILE_SALDO}: {email} | Saldo: R$ {saldo}", "SALDO")


def calib_focar_jogo_iframe(driver):
    """Localiza o iframe do jogo e muda o contexto do Selenium para ele."""
    try:
        driver.switch_to.default_content()
        iframes = driver.find_elements(By.TAG_NAME, "iframe")
        
        for i, iframe in enumerate(iframes):
            src = (iframe.get_attribute("src") or "").lower()
            if "history" in src or "login" in src: continue
            
            # Verifica se o iframe tem um CANVAS dentro (marca registrada do Cocos/WebGL)
            try:
                driver.switch_to.frame(iframe)
                canvases = driver.find_elements(By.TAG_NAME, "canvas")
                if len(canvases) > 0:
                    # log(f"Iframe {i} FOCO: Contém Canvas do Jogo.", "CONTEXTO")
                    return True
                driver.switch_to.default_content()
            except: 
                driver.switch_to.default_content()
                continue
                
            # Fallback por tamanho
            w_str = iframe.get_attribute("width") or "0"
            try: w = int(w_str.replace('px','').split('.')[0])
            except: w = 0
            if w > 300:
                driver.switch_to.frame(iframe)
                return True
    except Exception as e:
        log(f"Erro ao focar iframe: {e}", "CONTEXTO")
    return False

def calib_injetar_listener(driver):
    js = """
    (function() {
        if (window.__calibClicks !== undefined) return;
        window.__calibClicks = [];
        window.__calibListening = true;
        document.addEventListener('click', function(e) {
            if (!window.__calibListening) return;
            window.__calibClicks.push({x: e.clientX, y: e.clientY});
        }, true);
    })();
    """
    try:
        calib_focar_jogo_iframe(driver) # Garante que está no iframe do jogo antes de injetar
        driver.execute_script(js)
        log("Listener de cliques injetado no iframe do jogo.", "CALIB")
    except Exception as e:
        log(f"Aviso ao injetar listener: {e}", "CALIB")

def calib_aguardar_clique(driver, nome, timeout=120):
    calib_focar_jogo_iframe(driver) # Re-garante foco no iframe antes de esperar clique
    driver.execute_script("window.__calibClicks = []; window.__calibListening = true;")
    log(f"Aguardando clique em: '{nome}'...", "CALIB")
    start = time.time()
    while time.time() - start < timeout:
        try:
            cliques = driver.execute_script("return window.__calibClicks || [];")
            if cliques:
                c = cliques[0]
                x, y = int(c['x']), int(c['y'])
                driver.execute_script("window.__calibListening = false;")
                return x, y
        except: pass
        time.sleep(0.3)
    return None

def calib_inserir_codigo(driver, codigo):
    js = f"""
    (function() {{
        var el = document.getElementById('EditBoxId_1') || 
                 document.querySelector('.cocosEditBox') || 
                 document.querySelector('input[type="text"]') ||
                 document.querySelector('input');
        if (!el) return 'nao_encontrou';
        el.style.display = 'block';
        el.style.visibility = 'visible';
        el.focus();
        el.value = '{codigo}';
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        return 'inserido';
    }})();
    """
    try:
        calib_focar_jogo_iframe(driver) # MUITO IMPORTANTE: Garante foco no iframe para achar o EditBox
        res = driver.execute_script(js)
        log(f"Inserção de código: {res}", "CALIB")
        return 'inserido' in str(res)
    except Exception as e:
        log(f"Erro ao inserir código: {e}", "FALHA")
        return False

def calib_salvar_coords(coords):
    try:
        with open(ARQUIVO_COORDENADAS, 'w', encoding='utf-8') as f:
            json.dump({"coordenadas": coords, "timestamp": datetime.datetime.now().isoformat()}, f, indent=4)
        log(f"Coordenadas salvas em: {ARQUIVO_COORDENADAS}", "SUCESSO")
    except Exception as e:
        log(f"Erro ao salvar coords: {e}", "FALHA")

def calib_carregar_coords():
    if not os.path.exists(ARQUIVO_COORDENADAS):
        log(f"ERRO: {ARQUIVO_COORDENADAS} nao encontrado!", "FALHA")
        return None
    try:
        with open(ARQUIVO_COORDENADAS, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data.get("coordenadas", {})
    except: return None

def calib_clicar_coord(driver, x, y, nome=""):
    try:
        # LOG INICIAL para garantir visibilidade no terminal
        log(f"Tentando clique: '{nome}' → ({x}, {y})", "AUTO")
        
        calib_focar_jogo_iframe(driver) 
        js = f"""
        return (function() {{
            var el = document.elementFromPoint({x}, {y});
            if (!el) return 'nao_encontrado_ponto';
            var ev = {{bubbles: true, cancelable: true, clientX: {x}, clientY: {y}}};
            el.dispatchEvent(new MouseEvent('mousedown', ev));
            el.dispatchEvent(new MouseEvent('mouseup', ev));
            el.dispatchEvent(new MouseEvent('click', ev));
            return 'clicou:' + (el.tagName || 'unknown');
        }})();
        """
        res = driver.execute_script(js)
        log(f"Resultado '{nome}': {res}", "AUTO")
    except Exception as e:
        log(f"Erro ao clicar em '{nome}': {e}", "FALHA")

def calib_posicionar_grade(driver, indice):
    """Organiza janelas em uma grade de 2 fileiras por 5 colunas."""
    w, h = 384, 258
    # Ajuste do indice para 0-based
    idx = max(0, indice - 1)
    row = idx // 5
    col = idx % 5
    x = col * w
    y = row * h
    try:
        driver.set_window_rect(x, y, w, h)
        log(f"Janela posicionada na grade: Col {col}, Row {row} ({x}, {y})", "LAYOUT")
    except: pass

def calib_entrar_no_jogo(driver):
    """Clica no botão 'JOGUE' assim que ele aparece no lobby/play."""
    log("Aguardando botão 'JOGUE' para entrar no canvas...", "AUTO")
    if clicar(driver, SEL["jogue_button"], timeout=15, desc="JOGUE", skip_monitors=True):
        time.sleep(0.3)
        return True
    return False

def calib_aguardar_continuar_visual(driver, template="resources/continuar.png", threshold=0.97, timeout=60):
    """Aguarda o botao 'Continuar' aparecer visualmente no Canvas e trata CheckFace interrompendo o carregamento."""
    if not os.path.exists(template):
        log(f"Erro: {template} nao encontrado!", "FALHA")
        return False
    
    # 1. Espera inicial de 20s para o Canvas inicializar
    log(f"Iniciando espera de 20s para o Canvas inicializar...", "CALIB")
    start_inicial = time.time()
    while time.time() - start_inicial < 20:
        try:
            if 'checkFace' in driver.current_url:
                log("CheckFace detectado durante inicializacao. Abortando carregamento...", "AVISO")
                try: driver.set_window_size(480, 800)
                except: pass
                driver.back()
                return "checkface"
        except: pass
        time.sleep(1)
    
    log(f"Buscando botão Continuar (Precisão {threshold} | max {timeout}s)...", "CALIB")
    start = time.time()
    while time.time() - start < timeout:
        try:
            if 'checkFace' in driver.current_url:
                log("CheckFace detectado durante busca visual do Continuar. Abortando...", "AVISO")
                try: driver.set_window_size(480, 800)
                except: pass
                driver.back()
                return "checkface"

            driver.switch_to.default_content()
            scr_bytes = driver.get_screenshot_as_png()
            nparr = np.frombuffer(scr_bytes, np.uint8)
            img_tela = cv2.cvtColor(cv2.imdecode(nparr, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
            conf, cx, cy = _match_template(img_tela, template, threshold)
            
            if cx is not None:
                log(f"Botao detectado! Confirmação 1/2 (Conf={conf:.3f})", "SUCESSO")
                time.sleep(0.5)
                # Confirmacao 2 para garantir estabilidade
                scr_bytes = driver.get_screenshot_as_png()
                nparr = np.frombuffer(scr_bytes, np.uint8)
                img_tela2 = cv2.cvtColor(cv2.imdecode(nparr, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
                conf2, cx2, cy2 = _match_template(img_tela2, template, threshold)
                
                if cx2 is not None:
                    log("Botão Continuar ESTÁVEL na tela. Carregamento concluído.", "SUCESSO")
                    return (cx2, cy2)
        except: pass
        time.sleep(1.0)
    return False

def calib_aplicar_segundo_codigo(driver, perfil=""):
    """Aplica o segundo código de bônus após o primeiro terminar os giros."""
    coords = calib_carregar_coords()
    if not coords:
        log(f"[{perfil}] Coords não encontradas para segundo código.", "AVISO")
        return
    for acao in ["Engrenagem", "Presente"]:
        if acao in coords:
            calib_clicar_coord(driver, coords[acao][0], coords[acao][1], acao)
            time.sleep(2)
    calib_inserir_codigo(driver, CODIGO_BONUS_CALIB_2)
    time.sleep(2)
    if "Confirmar" in coords:
        calib_clicar_coord(driver, coords["Confirmar"][0], coords["Confirmar"][1], "Confirmar")
        if calib_verificar_sucesso_visual(driver):
            log(f"[{perfil}] SEGUNDO CÓDIGO VALIDADO COM SUCESSO!", "SUCESSO")
            calib_girar_rodadas(driver, perfil=perfil)
        else:
            log(f"[{perfil}] AVISO: Segundo código inserido, mas sucesso visual não detectado.", "AVISO")
    else:
        log(f"[{perfil}] Coords 'Confirmar' não encontradas para segundo código.", "AVISO")

def calib_executar_automacao(driver, indice, email="", password="", perfil=""):
    """Executa a automação completa: código → giros → saldo → fecha browser."""
    coords = calib_carregar_coords()
    if not coords: return

    try:
        max_tentativas_ip = 5
        resultado_final = "falha"

        for tentativa_ip in range(max_tentativas_ip):
            if check_stop(): break

            if tentativa_ip > 0:
                log(f"[Tentativa {tentativa_ip+1}/{max_tentativas_ip}] Refazendo automação pós-troca de IP...", "PROXY")
                # Restaura resolução inicial para navegar no lobby/play normal
                driver.set_window_size(480, 800)
                driver.switch_to.default_content()
                driver.get(URLS.get("play", driver.current_url))
                time.sleep(5)

            # Segurança constante: sempre trata CheckFace ao entrar ou re-entrar no /play
            if '/play' in driver.current_url or 'checkFace' in driver.current_url:
                if 'checkFace' in driver.current_url:
                    log("CheckFace detectado na transição. Restaurando 480x800 e Voltando...", "AVISO")
                    try: driver.set_window_size(480, 800)
                    except: pass
                    driver.back()
                    time.sleep(3)

            if tentativa_ip == 0:
                log("INICIANDO SEQUENCIA DE AUTOMAÇÃO...", "AUTO")
            
            # FASE DE ENTRADA (Loop de Retry se o CheckFace atrapalhar o JOGUE/Carregamento)
            tentativas_entrada = 0
            res_carga = False
            while tentativas_entrada < 3:
                tentativas_entrada += 1
                calib_entrar_no_jogo(driver)
                time.sleep(2)
                calib_redimensionar(driver)
                
                res_carga = calib_aguardar_continuar_visual(driver, timeout=60)
                if res_carga == "checkface":
                    log(f"Tentativa de entrada {tentativas_entrada} abortada por CheckFace. Re-tentando entrada...", "RETRY")
                    time.sleep(2)
                    continue 

                if res_carga and isinstance(res_carga, tuple):
                    cx, cy = res_carga
                    calib_clicar_coord(driver, cx, cy, "Continuar")
                    time.sleep(0.5)
                    break
                else:
                    log(f"Timeout aguardando Continuar (tentativa {tentativas_entrada}/3).", "FALHA")
            
            if not (res_carga and isinstance(res_carga, tuple)):
                resultado_final = "timeout_carregamento"
                break

            # Sequencia completa e automatizada de cliques internos
            for acao in ["Jogar", "Fechar evento", "Modo Turbo", "Engrenagem", "Presente"]:
                if acao in coords:
                    calib_clicar_coord(driver, coords[acao][0], coords[acao][1], acao)
                    if acao == "Jogar": time.sleep(15)
                    elif acao == "Fechar evento": time.sleep(6)
                    elif acao == "Modo Turbo": time.sleep(2)
                    else: time.sleep(2)

            # Inserir Código
            calib_inserir_codigo(driver, CODIGO_BONUS_CALIB)
            time.sleep(2)

            if "Confirmar" in coords:
                calib_clicar_coord(driver, coords["Confirmar"][0], coords["Confirmar"][1], "Confirmar")
                
                if calib_verificar_sucesso_visual(driver):
                    log(f"[{perfil}] CÓDIGO VALIDADO COM SUCESSO!", "SUCESSO")
                    res_giro = calib_girar_rodadas(driver, perfil=perfil)
                    # if res_giro != "ip_repetido":
                    #     calib_aplicar_segundo_codigo(driver, perfil=perfil)
                    resultado_final = res_giro if res_giro == "ip_repetido" else "sucesso"
                else:
                    log("AVISO: Código inserido, mas sucesso visual não detectado.", "AVISO")
                    resultado_final = "erro_visual"
                    break
            else:
                resultado_final = "erro_coords"
                break

            if resultado_final != "ip_repetido":
                break  # Sucesso ou outro erro - encerra tentativas
            
            # Se chegou aqui é IP REPETIDO:
            log(f"[{perfil}] IP repetido detectado. Rotacionando proxies...", "PROXY")
            global _time_ultima_rotacao_proxy
            with _lock_proxy_global:
                if time.time() - _time_ultima_rotacao_proxy > 45:
                    rotacionar_proxies()
                    _time_ultima_rotacao_proxy = time.time()
                else:
                    log("Proxies ja rotacionadas recentemente. Re-aproveitando...", "PROXY")
            # Loop recomeçará com novo IP

        # === FIM DA AUTOMAÇÃO (Apenas Saldo) ===
        if resultado_final == "sucesso":
            log(f"[{perfil}] Giros concluídos. Retornando para lobby.", "SUCESSO")
            # Agora sim volta para o lobby e ajusta resolução
            driver.get(URLS.get("play", driver.current_url))
            time.sleep(1.5)
            driver.set_window_size(480, 800)
            log(f"[{perfil}] Restaurada resolução 480x800 para saldo.", "DIAG")
            # Tira checkface se tiver aparecido
            if 'checkFace' in driver.current_url:
                log("CheckFace detectado na tela de saldo. Restaurando 480x800 e Voltando...", "AVISO")
                try: driver.set_window_size(480, 800)
                except: pass
                driver.back()
                time.sleep(1)

            saldo = verificar_saldo(driver)
            salvar_conta_com_saldo(email, password, perfil, saldo)
            log(f"Automacao concluida. Conta: {email} | Saldo: R$ {saldo}", "SUCESSO")
            # Nao fechar o driver aqui — processar() e o unico ponto de cleanup

    finally:
        try:
            driver.set_window_size(480, 800)
        except: pass

def _match_template(img_cinza, template_path, threshold, escalas=[0.85, 1.0, 1.15]):
    """Auxiliar: retorna (conf, cx, cy) ou (0, None, None) se nao encontrar."""
    img_modelo = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
    if img_modelo is None: return 0, None, None
    melhor_val, melhor_pos, melhor_esc = 0, None, 1.0
    for s in escalas:
        h, w = img_modelo.shape[:2]
        tpl = cv2.resize(img_modelo, (int(w*s), int(h*s)), interpolation=cv2.INTER_AREA)
        if tpl.shape[0] > img_cinza.shape[0] or tpl.shape[1] > img_cinza.shape[1]: continue
        res = cv2.matchTemplate(img_cinza, tpl, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if max_val > melhor_val:
            melhor_val, melhor_pos, melhor_esc = max_val, max_loc, s
    if melhor_val >= threshold and melhor_pos:
        h_tpl = int(cv2.imread(template_path, cv2.IMREAD_GRAYSCALE).shape[0] * melhor_esc)
        w_tpl = int(cv2.imread(template_path, cv2.IMREAD_GRAYSCALE).shape[1] * melhor_esc)
        cx = melhor_pos[0] + w_tpl // 2
        cy = melhor_pos[1] + h_tpl // 2
        return melhor_val, cx, cy
    return melhor_val, None, None

def verificar_iprepetido(driver, template="resources/iprepetido.png", threshold=0.80):
    """Verifica se a caixa de presente com erro de IP repetido esta visivel."""
    if not os.path.exists(template):
        return False
    try:
        driver.switch_to.default_content()
        scr_bytes = driver.get_screenshot_as_png()
        nparr = np.frombuffer(scr_bytes, np.uint8)
        img_tela = cv2.cvtColor(cv2.imdecode(nparr, cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
        conf, _, _ = _match_template(img_tela, template, threshold)
        if conf >= threshold:
            log(f"IP REPETIDO detectado! (conf={conf:.2f})", "PROXY")
            return True
        return False
    except Exception as e:
        log(f"Erro ao verificar iprepetido: {e}", "FALHA")
        return False

def calib_girar_rodadas(driver, perfil=""):
    """
    Loop visual para clicar no botão de girar e detectar fim de giros.
    - Clica enquanto botaogirar for detectado (conf >= 0.90).
    - Para IMEDIATAMENTE ao detectar semgiro.png (sem rodadas restantes).
    - Sem cliques desnecessários.
    """
    template = "resources/botaogirar.png"
    template_sem = "resources/semgiro.png"
    threshold = 0.88
    intervalo = 0.8
    max_rodadas = 500 # Limite de segurança bem alto, o visual quem manda
    
    log(f"[{perfil}] Iniciando automação de giros (modo visual total)...", "GIRAR")
    
    # FASE 1: Espera o botão aparecer (até 5 segundos)
    botao_encontrado = False
    for _ in range(10):  # 10 tentativas x 0.5s = 5 segundos
        try:
            driver.switch_to.default_content()
            scr_bytes = driver.get_screenshot_as_png()
            nparr = np.frombuffer(scr_bytes, np.uint8)
            img_tela_color = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            img_tela = cv2.cvtColor(img_tela_color, cv2.COLOR_BGR2GRAY)
            conf, cx, cy = _match_template(img_tela, template, threshold)
            if cx is not None:
                botao_encontrado = True
                log(f"Botao de girar detectado! (conf={conf:.2f}). Iniciando giros.", "GIRAR")
                break
        except: pass
        time.sleep(0.5)
    
    if not botao_encontrado:
        log("Botao nao apareceu em 5s. Verificando IP repetido...", "GIRAR")
        if verificar_iprepetido(driver):
            log("IP REPETIDO detectado (caixa ainda aberta)!", "PROXY")
            return "ip_repetido"
        log("Sem IP repetido. Nenhuma rodada disponivel.", "GIRAR")
        return

    # FASE 2: Loop de giros
    rodadas_clicadas = 0
    segundos_sem_botao = 0
    confirmacoes_sem_giro = 0
    
    while rodadas_clicadas < max_rodadas:
        try:
            # Garante que a janela está ativa para o WebGL não suspender
            try: driver.execute_script("window.focus();")
            except: pass
            
            driver.switch_to.default_content()
            scr_bytes = driver.get_screenshot_as_png()
            nparr = np.frombuffer(scr_bytes, np.uint8)
            img_tela_color = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            img_tela = cv2.cvtColor(img_tela_color, cv2.COLOR_BGR2GRAY)
            
            # 1. Verifica semgiro.png - PRECISA DE 3 CONFIRMAÇÕES para parar
            if os.path.exists(template_sem):
                conf_sem, _, _ = _match_template(img_tela, template_sem, threshold)
                if conf_sem >= threshold:
                    confirmacoes_sem_giro += 1
                    if confirmacoes_sem_giro >= 3:
                        log(f"[{perfil}] Sem giro detectado (confirmado 3x). Total: {rodadas_clicadas}", "GIRAR")
                        break
                    time.sleep(0.5)
                    continue
                else:
                    confirmacoes_sem_giro = 0
            
            # 2. Verifica botaogirar.png
            conf_girar, cx, cy = _match_template(img_tela, template, threshold)
            if cx is not None:
                segundos_sem_botao = 0
                js = f"""
                (function() {{
                    var el = document.elementFromPoint({cx}, {cy});
                    if (!el) return 'nao_encontrado';
                    var ev = {{bubbles: true, cancelable: true, clientX: {cx}, clientY: {cy}}};
                    el.dispatchEvent(new MouseEvent('mousedown', ev));
                    el.dispatchEvent(new MouseEvent('mouseup', ev));
                    el.dispatchEvent(new MouseEvent('click', ev));
                    return 'clicou:' + el.tagName;
                }})();
                """
                try: driver.execute_script(js)
                except: pass
                rodadas_clicadas += 1
                log(f"[{perfil}] Rodada {rodadas_clicadas}: conf={conf_girar:.2f}", "GIRAR")
                time.sleep(intervalo)
            else:
                segundos_sem_botao += 1
                # Checagem imediata de IP repetido se o botão sumiu
                if verificar_iprepetido(driver):
                    log(f"[{perfil}] IP REPETIDO detectado durante giros!", "PROXY")
                    return "ip_repetido"
                    
                # Log de status frequente para depuração
                if segundos_sem_botao % 10 == 0:
                    log(f"[{perfil}] Buscando botão de girar... (IP OK)", "DIAG")
                
                # Se demorar muito (60s), pode ser um popup genérico não mapeado
                if segundos_sem_botao >= 60:
                    log(f"[{perfil}] AVISO: 60s sem ver botão. Pode haver popup bloqueando.", "AVISO")
                    segundos_sem_botao = 0 
                
                time.sleep(1.0)
                
        except Exception as e:
            log(f"[{perfil}] Erro no loop de girar: {e}", "FALHA")
            time.sleep(1)


    log(f"Loop encerrado. Total de giros: {rodadas_clicadas}", "GIRAR")


def calib_verificar_sucesso_visual(driver, imagem_ref="print_vitoria.png", threshold=0.80, duracao=4):
    """Verificação visual aprimorada: Multiescala, Escala de Cinza e Polling rápido."""
    if not os.path.exists(imagem_ref):
        log(f"Erro: {imagem_ref} nao encontrada para template matching.", "FALHA")
        return False
        
    # Carrega e prepara o template (escala de cinza)
    img_modelo = cv2.imread(imagem_ref, cv2.IMREAD_GRAYSCALE)
    if img_modelo is None: return False
    
    log(f"Iniciando busca visual por '{imagem_ref}'...", "VISUAL")
    inicio = time.time()
    melhor_conf = 0
    
    while (time.time() - inicio) < duracao:
        try:
            driver.switch_to.default_content() 
            scr_bytes = driver.get_screenshot_as_png()
            nparr = np.frombuffer(scr_bytes, np.uint8)
            img_tela_color = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            img_tela = cv2.cvtColor(img_tela_color, cv2.COLOR_BGR2GRAY)
            
            # 1. TEMPLATE MATCHING MULTIESCALA (Tenta 0.9, 1.0, 1.1 para compensar zoom/dpi)
            scales = [0.9, 1.0, 1.1]
            for s in scales:
                if s == 1.0:
                    tpl = img_modelo
                else:
                    h, w = img_modelo.shape[:2]
                    tpl = cv2.resize(img_modelo, (int(w*s), int(h*s)), interpolation=cv2.INTER_AREA)
                
                if tpl.shape[0] > img_tela.shape[0] or tpl.shape[1] > img_tela.shape[1]:
                    continue
                    
                res = cv2.matchTemplate(img_tela, tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, _ = cv2.minMaxLoc(res)
                if max_val > melhor_conf: melhor_conf = max_val
                
                if max_val >= threshold:
                    log(f"SUCESSO: Imagem '{imagem_ref}' detectada! Confiança: {max_val:.3f} (escala {s})", "VISUAL")
                    return True

            # 2. SEGUNDO CHECK: TEXTO EM TODOS OS FRAMES (Fallback)
            if melhor_conf < threshold:
                iframes = driver.find_elements(By.TAG_NAME, "iframe")
                contextos = [None] + iframes # None representa default content
                palavras = ["Rodadas", "Grátis", "Parabéns", "venceu", "sucesso"]
                
                for ctx in contextos:
                    try:
                        if ctx: driver.switch_to.frame(ctx)
                        else: driver.switch_to.default_content()
                        
                        for p in palavras:
                            xpath = f"//*[contains(translate(text(), 'ABCDEFGHIJKLMNOPQRSTUVWXYZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚ', 'abcdefghijklmnopqrstuvwxyzàáâãèéêìíòóôõùú'), '{p.lower()}')]"
                            if len(driver.find_elements(By.XPATH, xpath)) > 0:
                                log(f"SUCESSO: Texto vitória '{p}' detectado no frame!", "VISUAL")
                                return True
                    except: continue
        except: pass
        
        time.sleep(0.3) # Polling rápido
    
    log(f"Fim da busca. Melhor confiança OpenCV: {melhor_conf:.3f} (Mínimo: {threshold})", "VISUAL")
    return False

def processar(indice):
    resultado_conta = carregar_conta()
    if not resultado_conta[0]: return False, None, False
    email, password, perfil = resultado_conta

    driver = None
    proxy_str = None
    sucesso = False
    login_sucesso = False

    # Tenta criar o driver com até 3 proxies diferentes
    MAX_PROXY_TENTATIVAS = 3
    for _pt in range(MAX_PROXY_TENTATIVAS):
        proxy_str = obter_proxy()
        if proxy_str:
            log(f"[{perfil}] Proxy tentativa {_pt+1}/{MAX_PROXY_TENTATIVAS}: {proxy_str.split(':')[0]}:{proxy_str.split(':')[1]}", "PROXY")
        else:
            log(f"[{perfil}] Sem proxy disponivel. Rodando sem proxy.", "AVISO")
        driver = criar_driver(email, indice, perfil, proxy_str)
        if driver:
            break
        log(f"[{perfil}] Driver falhou (tentativa {_pt+1}/{MAX_PROXY_TENTATIVAS}). {'Tentando proxima proxy...' if _pt+1 < MAX_PROXY_TENTATIVAS else 'Desistindo da conta.'}", "AVISO")
        time.sleep(5)

    try:
        if not driver:
            liberar_conta_falha(email)
            return False, perfil, True

        for tentativa in range(1, 3):
            log(f"[{perfil}] Tentativa {tentativa} de Login...", "DIAG")

            if URLS["login"] not in driver.current_url:
                driver.get(URLS["login"])
                time.sleep(2)

            # Verifica se já tem popup aberto antes de clicar
            popup_existente = False
            for handle in driver.window_handles:
                try:
                    driver.switch_to.window(handle)
                    if "accounts.google.com" in driver.current_url:
                        popup_existente = True
                        break
                except: pass
            
            driver.switch_to.window(driver.window_handles[0])

            if popup_existente:
                log("Detectado popup do Google aberto na tentativa inicial. Tratando...", "DIAG")
                tratar_popup_google(driver, email, password)
            elif clicar(driver, SEL["google_login_button"], timeout=15, desc="Botao Google", skip_monitors=True):
                tratar_popup_google(driver, email, password)

            try:
                if len(driver.window_handles) > 0:
                    driver.switch_to.window(driver.window_handles[0])
            except: pass

            if logado_check(driver, timeout=45):
                log(f"[{perfil}] LOGIN RECONHECIDO COM SUCESSO!", "DIAG")
                login_sucesso = True
                # FIX: salvar_sucesso movido para APOS /play ser confirmado (ver abaixo)
                # Aqui apenas marca o login, nao salva ainda

                log("Indo para o jogo...", "DIAG")
                driver.get(URLS['gameList'])

                login_ok = True
                sucesso = False
                
                # LOOP DE PERSISTÊNCIA: Tenta achar o jogo, mas se deslogar, faz re-login e continua
                for tentativa_lobby in range(6):
                    if check_stop(): break
                    
                    if not login_ok:
                        log(f"[{perfil}] SESSAO CAIU. Refazendo login...", "RETRY")
                        driver.get(URLS['login'])
                        time.sleep(1.5)
                        # 0. Verifica se já tem popup aberto antes de clicar
                        popup_existente = False
                        for handle in driver.window_handles:
                            try:
                                driver.switch_to.window(handle)
                                if "accounts.google.com" in driver.current_url:
                                    popup_existente = True
                                    break
                            except: pass
                        
                        driver.switch_to.window(driver.window_handles[0])
                        
                        if popup_existente:
                            log("Detectado popup do Google aberto. Tratando re-login...", "DIAG")
                            tratar_relogin_google(driver, email, password)
                        elif clicar(driver, SEL["google_login_button"], desc="Botao Google (Re-Login)"):
                            tratar_relogin_google(driver, email, password)

                        try:
                            if len(driver.window_handles) > 0:
                                driver.switch_to.window(driver.window_handles[0])
                        except: pass
                        time.sleep(3)
                        login_ok = True
                        driver.get(URLS['gameList'])
                        time.sleep(2)

                    # 1. Espera o lobby carregar ou detecta CheckFace/Login
                    lobby_estavel = False
                    for _ in range(10):
                        if 'checkFace' in driver.current_url:
                            log("CheckFace detectado no lobby. Restaurando resolucao 480x800 e Voltando...", "AVISO")
                            try: driver.set_window_size(480, 800)
                            except: pass
                            driver.back()
                            time.sleep(2)
                            continue
                        if procurar_em_iframes(driver, SEL.get("login_button_check", [])):
                            login_ok = False
                            break
                        if procurar_em_iframes(driver, SEL["load_more_1"]) or procurar_em_iframes(driver, SEL["target_game"]):
                            lobby_estavel = True
                            break
                        time.sleep(1)
                    
                    if not login_ok: continue # Volta para o início do for para re-logar

                    # 2. Busca o jogo (pode retornar "RELOGIN", True ou False)
                    resultado_busca = buscar_jogo_scroll(driver)
                    
                    if resultado_busca == "RELOGIN":
                        login_ok = False
                        continue # Re-loga
                    
                    if resultado_busca is True:
                        # Entrou no jogo! Agora monitora o /play
                        for _ in range(40):
                            if check_stop(): break
                            url_jogo_atual = driver.current_url
                            
                            if 'checkFace' in url_jogo_atual:
                                log("CheckFace detectado no jogo! Restaurando resolucao 480x800 e Voltando...", "AVISO")
                                try: driver.set_window_size(480, 800)
                                except: pass
                                driver.back()
                                time.sleep(1.5)
                                break # Sai do monitoramento para re-tentar busca ou re-logar

                            if '/play' in url_jogo_atual:
                                log(f"[{perfil}] JOGO CARREGADO NO /PLAY.", "DIAG")
                                salvar_sucesso(email, password, perfil)
                                
                                # =================================================
                                #  LÓGICA DE JOGO (CALIBRAÇÃO OU AUTOMAÇÃO)
                                # =================================================
                                if MODO_CALIBRACAO:
                                    log("INICIANDO MODO CALIBRAÇÃO...", "CALIB")
                                    # O clique no JOGUE é parte da calibração também
                                    calib_entrar_no_jogo(driver)
                                    time.sleep(2)
                                    calib_redimensionar(driver)
                                    log("Aguardando 30 segundos...", "CALIB")
                                    time.sleep(30)
                                    calib_injetar_listener(driver)
                                    
                                    coords_final = {}
                                    for acao in ACAO_CALIBRACAO:
                                        res = calib_aguardar_clique(driver, acao)
                                        if res:
                                            coords_final[acao] = res
                                            log(f"{acao} → {res}", "CALIB")
                                    
                                    # Chat
                                    print("\n" + "="*40)
                                    input("Posso digitar o código no chat? Pressione ENTER para continuar")
                                    calib_inserir_codigo(driver, CODIGO_BONUS_CALIB)
                                    
                                    # Confirmar
                                    print("="*40)
                                    input("Posso iniciar calibração do botão CONFIRMAR? Pressione ENTER")
                                    res_conf = calib_aguardar_clique(driver, "Confirmar")
                                    if res_conf:
                                        coords_final["Confirmar"] = res_conf
                                        log(f"Confirmar → {res_conf}", "CALIB")
                                    
                                    calib_salvar_coords(coords_final)
                                    log("CALIBRAÇÃO CONCLUÍDA!", "SUCESSO")
                                else:
                                    # MODO AUTOMAÇÃO (USA DELAYS DO ÁUDIO)
                                    try:
                                        calib_executar_automacao(driver, indice, email=email, password=password, perfil=perfil)
                                        sucesso = True
                                    except Exception as e_auto:
                                        log(f"Erro na automação: {e_auto}", "FALHA")
                                # =================================================

                                break
                            time.sleep(0.5)
                        
                        if sucesso: break
                    else:
                        log(f"[{perfil}] Jogo nao encontrado na tentativa {tentativa_lobby+1}. Recarregando lobby...", "AVISO")
                        driver.get(URLS["gameList"])
                        time.sleep(2)

                break

                break
            else:
                log(f"[{perfil}] Tentativa {tentativa} falhou.", "DIAG")
                time.sleep(3)

    except Exception as e:
        log(f"Erro Geral [{perfil}]: {e}", "FALHA")
    finally:
        # FIX: Garante que o email SEMPRE sai do set em_progresso ao terminar a thread
        if not sucesso:
            liberar_conta_falha(email)

    # Fecha o driver (unico ponto de quit — evita double-quit e timeout de 30s)
    if driver:
        try:
            driver.quit()
        except: pass

    # Limpa perfil em background para nao atrasar o proximo batch
    _perfil_path = os.path.abspath(f"UserData/{perfil}")
    threading.Thread(target=limpar_perfil, args=(_perfil_path,), daemon=True).start()

    return (sucesso or login_sucesso), perfil, True

def processar_wrapper(indice):
    email_wrapper = None  # Rastreia email para liberar em caso de falha catastrofica
    try:
        resultado = processar(indice)
        if not isinstance(resultado, tuple) or len(resultado) < 3:
            return
            
        sucesso_final, perfil, conta_encontrada = resultado
        
        if not conta_encontrada:
            log(f"Thread {indice}: Nenhuma conta disponível.", "INFO")
            return
            
        perfil_log = perfil if perfil else f"T_{indice}"
        
        # Se 'sucesso_final' for True, significa que o /play foi confirmado
        # e o navegador foi mantido aberto.
        if sucesso_final:
            log(f"[{perfil_log}] Fluxo concluído com sucesso. Navegador aberto no jogo.", "SUCESSO")
        else:
            log(f"[{perfil_log}] Fluxo falhou. Conta permanece no JSON para reprocessamento.", "AVISO")
    except Exception as e:
        log(f"Erro catastrofico no wrapper (Thread {indice}): {e}", "FALHA")
        # FIX: Garante liberação do set mesmo em falha catastrófica do wrapper
        # (o finally de processar() ja cobre o caso normal)
        log(f"Thread {indice} finalizada com erro. Conta sera reprocessada na proxima execucao.", "INFO")

def logado_check(driver, timeout=10):
    start = time.time()
    while time.time() - start < timeout:
        # Monitoramento Passivo de Popup de Bônus (pode aparecer a qualquer momento)
        verificar_popup_bonus(driver)

        # 1. Verifica Popup de CPF (Sucesso absoluto - Conta Criada)
        try:
            if procurar_em_iframes(driver, SEL.get("popup_cpf_sucesso", [])):
                log("Popup de CPF encontrado! Conta criada com sucesso.", "SUCESSO")
                return True
        except: pass

        # 2. Monitoramento de URL (checkFace)
        if 'checkFace' in driver.current_url:
            log("Detectado checkFace na URL. Restaurando 480x800 e Tentando voltar...", "AVISO")
            try: 
                driver.set_window_size(480, 800)
                driver.back()
            except: pass
            time.sleep(2)

        # 3. Verifica elemento de usuário logado (cabeçalho)
        try:
            if driver.find_elements(By.CSS_SELECTOR, ".vux-header-right .user"):
                return True
        except: pass

        # 4. Fecha modais irrelevantes (apenas se atrapalhar, mas aqui priorizamos o CPF)
        for by_str, val in SEL.get("modal_close", []):
            try: 
                m = driver.find_elements(by_str, val)
                if m and m[0].is_displayed():
                    if 'icon-close' in val: 
                        m[0].click()
                        # Se fechou um modal, pode ser que esteja logado, mas vamos esperar confirmar pelo CPF ou Header
            except: pass
        
        time.sleep(1)
    return False

if __name__ == "__main__":
    if os.path.exists(STOP_FILE): os.remove(STOP_FILE)

    log("Rotacionando IPs antes de iniciar (garantindo IPs novos)...", "PROXY")
    rotacionar_proxies()
    
    # Pergunta interativa com flush para garantir exibição
    print("\n" + "="*40, flush=True)
    print(f"PADRAO CONFIGURADO: {QUANTIDADE_DE_CONTAS} contas.", flush=True)
    print("Digite a quantidade desejada e pressione ENTER.", flush=True)
    print("Ou apenas pressione ENTER para usar o padrao.", flush=True)
    print("="*40, flush=True)
    
    # Configuração de Índices
    INDICE_INICIAL = 1
    
    try:
        # Input Inteligente
        print(">> Quantidade: ", end='', flush=True)
        q_input = input()
        if q_input.strip():
            QUANTIDADE_DE_CONTAS = int(q_input.strip())
            
        print(f"-> Definido: {QUANTIDADE_DE_CONTAS} contas na fila.", flush=True)
            
    except Exception as e:
        print(f"-> Usando padrao ({e}).", flush=True)

    time.sleep(1) # Pausa dramática
    log(f"Iniciando loop para {QUANTIDADE_DE_CONTAS} contas com 2 navegadores.")
    
    import concurrent.futures
    
    lista_indices = list(range(INDICE_INICIAL, INDICE_INICIAL + QUANTIDADE_DE_CONTAS))
    
    proxies_usadas_total = 0
    with ThreadPoolExecutor(max_workers=3) as executor:
        for b in range(0, len(lista_indices), 3):
            if check_stop(): break

            batch = lista_indices[b:b+3]
            log(f"Iniciando LOTE de {len(batch)} contas (Índices: {batch}).", "BATCH")
            
            # Submete o par de navegadores (Execução Rápida)
            futures = [executor.submit(processar_wrapper, i) for i in batch]
            
            # Aguarda o par terminar completamente
            concurrent.futures.wait(futures)

            proxies_usadas_total += len(batch)

            if check_stop(): break

            # Rotaciona proxies quando ciclo completo (todos os IPs usados)
            if _proxies_lista and b + 3 < len(lista_indices):
                if proxies_usadas_total > 0 and proxies_usadas_total % len(_proxies_lista) == 0:
                    log(f"Ciclo completo ({len(_proxies_lista)} proxies usadas). Rotacionando IPs...", "PROXY")
                    rotacionar_proxies()

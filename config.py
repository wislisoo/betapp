from pathlib import Path

# =============================================================================
# CONFIGURAÇÕES — Preencha antes de rodar o orchestrator.py
# =============================================================================

# --- Navegadores ---
HEADLESS         = True    # True = --headless=new (sem Xvfb) | False = Xvfb
USE_PROXY        = False   # True = roteia tráfego do jogo pelo WebShare (proxies.txt)
MAX_ACCOUNTS     = 15      # Quantas contas usar (máx. 50 disponíveis)
LAUNCH_BATCH     = 2      # Quantos browsers abrir por vez (evita pico de RAM)
HEADLESS_STAGGER = 2      # Segundos entre cada browser no modo headless (reduz pico de CPU)
CPU_LIMIT_RENDERER = 15   # % de CPU máximo por processo --type=renderer (Linux, via cpulimit)
CPU_LIMIT_GPU      = 20   # % de CPU máximo por processo --type=gpu-process (Linux, via cpulimit)
MEMORY_LIMIT_BROWSER_MB = 2000   # MB total (browser + renderers + GPU) por instância Chrome
CHROME_MAX_AGE_HOURS = 6    # Reinicia Chrome após X horas de uptime (evita acúmulo de GPU/WebGL)

# --- Timeouts (segundos) ---
LOGIN_TIMEOUT    = 30
NAV_TIMEOUT      = 45
SELECTOR_TIMEOUT = 15
GAME_URL_WAIT    = 120   # segundos aguardando URL do jogo WB (se abrir página errada)

# --- URLs ---
LOGIN_URL    = "https://bet.app/login"
GAMELIST_URL = "https://bet.app/gameList?flag=103"

# Preenchidos automaticamente pelo game_cdp ao capturar o Token da sessão
API_URL    = ""   # ex: "https://wbslot-fd-na.nufzelo.tr/fg/req"
API_ORIGIN = ""   # ex: "https://wbgame.nufzelo.tr"

# --- Testador de códigos ---
SUCCESS_THRESHOLD    = 100   # body > 100 bytes = código válido
RATE_LIMIT_THRESHOLD = 10    # body < 10 bytes  = bloqueio real
STALE_RESPONSE_MAX   = 45    # body entre RATE_LIMIT e este valor = sessão morta (37b) → reload
CODE_PREFIX  = "BETAPP"
CODE_CHARSET = "abcdefghijklmnopqrstuvwxyz0123456789"
CODE_LENGTH  = 6

# Firebase — coordenação de códigos entre múltiplos usuários
FIREBASE_URL      = "https://db-vermelho-default-rtdb.firebaseio.com/"    # ex: "https://SEU-PROJETO-default-rtdb.firebaseio.com"
FIREBASE_KEY_PATH = str(Path(__file__).parent / "db-vermelho.json")
FIREBASE_BATCH    = 100   # quantos índices reivindicar por vez

# Cooldowns (segundos) para evitar bloqueio de 3 tentativas
COOLDOWN_PREVENTION = 0.1    # pausa após 2 inválidos seguidos (antes do 3º)
COOLDOWN_RECOVERY   = 6.0   # pausa se bloqueio foi acionado mesmo assim
# --- Jogo ---
GAME_IMAGE_URL_FRAGMENT = "551476"   # parte da URL da imagem do Fortune Gems

# --- Calibração visual (offset X/Y em pixels sobre o centro do template) ---
# Ajuste os valores abaixo se o clique não estiver batendo no botão certo
CALIB_CLOSE_EVENT = (342, 22)    # botão fechar evento (após 8s)
CALIB_GEAR        = (418, 624)   # botão engrenagem
CALIB_PRESENT     = (417, 529)   # botão presente
CALIB_CONFIRMAR   = (200, 511)   # botão Confirmar da caixa de presente

# --- Matching visual ---
VISUAL_MATCHING  = 0.97   # confiança mínima para aceitar template (0-1)
VISUAL_WAIT      = 10     # segundos máximos esperando o template aparecer

# --- Google OAuth ---
GOOGLE_BTN_SEL = "span.gg"

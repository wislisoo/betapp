import threading
from pathlib import Path
from dataclasses import dataclass

PROFILES_DIR     = Path(__file__).parent / "profiles"
ACCOUNTS_TXT     = Path(__file__).parent / "accounts.txt"
ACCOUNTS_CRIADAS = Path(__file__).parent / "accounts_criadas.txt"

_in_progress: set = set()
_lock = threading.Lock()


@dataclass
class Account:
    index: int
    login: str
    senha: str

    @property
    def profile_dir(self) -> Path:
        return PROFILES_DIR / f"account_{self.index:02d}"


def _created_emails() -> set:
    if not ACCOUNTS_CRIADAS.exists():
        return set()
    lines = ACCOUNTS_CRIADAS.read_text(encoding="utf-8").splitlines()
    return {line.split("|")[0].strip().lower() for line in lines if "|" in line}


def load_accounts_from_txt(limit: int = 50) -> list["Account"]:
    """Lê accounts.txt (formato: index|email|password) → lista de Account.

    Usa o índice real da coluna 0 para que profile_dir case com account_NN nas pastas.
    """
    if not ACCOUNTS_TXT.exists():
        return []
    accounts = []
    for line in ACCOUNTS_TXT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0].strip())
        except ValueError:
            idx = len(accounts) + 1  # fallback se coluna 0 não for número
        email, password = parts[1].strip(), parts[2].strip()
        if email:
            accounts.append(Account(index=idx, login=email, senha=password))
        if len(accounts) >= limit:
            break
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    return accounts


def load_accounts_by_range(start: int, end: int) -> list["Account"]:
    """Carrega contas cujo índice está entre start e end (inclusive).

    Critério duplo:
      1. O índice da coluna 0 do accounts.txt deve estar no range [start, end].
      2. A pasta profiles/account_NN deve existir na VPS (tem o cache do Chrome).

    Exemplos:
        load_accounts_by_range(1, 5)   →  account_01 … account_05
        load_accounts_by_range(6, 10)  →  account_06 … account_10
    """
    import logging
    _log = logging.getLogger("timbet")

    if not ACCOUNTS_TXT.exists():
        return []

    accounts: list["Account"] = []
    for line in ACCOUNTS_TXT.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0].strip())
        except ValueError:
            continue
        if idx < start or idx > end:
            continue
        email, password = parts[1].strip(), parts[2].strip()
        if not email:
            continue
        acc = Account(index=idx, login=email, senha=password)
        if not acc.profile_dir.exists():
            _log.warning(
                f"[{idx:02d}] Pasta '{acc.profile_dir}' não encontrada — pulando conta"
            )
            continue
        accounts.append(acc)

    accounts.sort(key=lambda a: a.index)
    return accounts


def pop_next_account(reuse_index: int) -> "Account | None":
    """Retorna próxima conta disponível do TXT e marca como em-uso (thread-safe)."""
    with _lock:
        if not ACCOUNTS_TXT.exists():
            return None
        already_created = _created_emails()
        for line in ACCOUNTS_TXT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            email, password = parts[1].strip().lower(), parts[2].strip()
            if email in already_created or email in _in_progress:
                continue
            _in_progress.add(email)
            return Account(index=reuse_index, login=parts[1].strip(), senha=password)
        return None


def save_account_success(account: "Account") -> None:
    """Remove de accounts_teste.txt e salva em accounts_criadas.txt."""
    with _lock:
        _in_progress.discard(account.login.lower())

        if ACCOUNTS_TXT.exists():
            lines = ACCOUNTS_TXT.read_text(encoding="utf-8").splitlines()
            lines = [l for l in lines if account.login.lower() not in l.lower()]
            ACCOUNTS_TXT.write_text(
                "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
            )

        with open(ACCOUNTS_CRIADAS, "a", encoding="utf-8") as f:
            f.write(f"{account.login}|{account.senha}\n")


def release_account(account: "Account") -> None:
    """Libera conta do set em-progresso quando setup falha."""
    _in_progress.discard(account.login.lower())

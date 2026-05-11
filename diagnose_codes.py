"""
diagnose_codes.py — Extrai todos os scripts JavaScript da página do jogo
e procura por referências a códigos promocionais, validação, redempção.

Uso: python diagnose_codes.py <porta_chrome> <tab_id_ou_url_fragmento>
  Ex: python diagnose_codes.py 9316 551476
      python diagnose_codes.py 9316 wbslot
"""

import sys
import json
import re
import pychrome
import requests
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("diagnose")

KEYWORDS = [
    "promo", "code", "gift", "redeem", "claim", "voucher", "coupon",
    "valid", "verify", "check", "enter", "submit", "send", "apply",
    "bonus", "reward", "present", "resgate", "codigo", "cupom",
    "promoCode", "giftCode", "redeemCode", "bonusCode",
    "promo_code", "gift_code", "redeem_code",
]


def list_tabs(port: int) -> list[dict]:
    resp = requests.get(f"http://127.0.0.1:{port}/json")
    return resp.json()


def find_game_tab(port: int, fragment: str) -> str | None:
    tabs = list_tabs(port)
    for t in tabs:
        if fragment in t.get("url", ""):
            return t["id"]
    return None


def extract_scripts(port: int, tab_id: str) -> list[dict]:
    """Extrai TODOS os scripts (inline + externos) da aba do jogo."""
    browser = pychrome.Browser(url=f"http://127.0.0.1:{port}")
    tab = None
    for t in browser.list_tab():
        if t.id == tab_id:
            tab = t
            break
    if not tab:
        log.error(f"Aba {tab_id} não encontrada")
        return []

    tab.start()
    tab.Page.enable()
    tab.Runtime.enable()

    # Coleta todos os scripts da página
    result = tab.Runtime.evaluate(expression="""
    (function() {
        var scripts = [];
        // Scripts externos (src)
        Array.from(document.scripts).forEach(function(s, i) {
            if (s.src) {
                scripts.push({type: 'external', index: i, src: s.src});
            }
        });
        // Scripts inline e conteúdo de externos cacheados
        // Tenta pegar fonte via Performance API
        if (performance && performance.getEntriesByType) {
            var entries = performance.getEntriesByType('resource');
            entries.forEach(function(e) {
                if (e.name.endsWith('.js') || e.initiatorType === 'script') {
                    scripts.push({type: 'perf_entry', name: e.name, duration: e.duration});
                }
            });
        }
        return JSON.stringify(scripts);
    })();
    """)

    scripts_info = json.loads(result["result"]["value"])

    # Para cada script externo, tenta buscar o conteúdo
    full_scripts = []
    for info in scripts_info:
        src = info.get("src") or info.get("name") or ""
        if not src or not src.endswith(".js"):
            continue
        try:
            content_result = tab.Runtime.evaluate(expression=f"""
            (function() {{
                try {{
                    var xhr = new XMLHttpRequest();
                    xhr.open('GET', '{src}', false);
                    xhr.send();
                    if (xhr.status === 200) {{
                        return xhr.responseText;
                    }}
                }} catch(e) {{}}
                return '';
            }})();
            """)
            content = content_result.get("result", {}).get("value", "")
            if content:
                full_scripts.append({"src": src, "size": len(content), "content": content})
                log.info(f"Script carregado: {src} ({len(content)} bytes)")
        except Exception as e:
            log.warning(f"Falha ao carregar {src}: {e}")

    # Também captura todos os scripts inline
    inline_result = tab.Runtime.evaluate(expression="""
    (function() {
        var inline = [];
        Array.from(document.scripts).forEach(function(s, i) {
            if (!s.src && s.textContent) {
                inline.push({type: 'inline', index: i, content: s.textContent});
            }
        });
        return JSON.stringify(inline);
    })();
    """)

    try:
        inline_scripts = json.loads(inline_result["result"]["value"])
        for s in inline_scripts:
            full_scripts.append({
                "src": f"inline_script_{s['index']}",
                "size": len(s["content"]),
                "content": s["content"],
            })
            log.info(f"Script inline #{s['index']} ({len(s['content'])} bytes)")
    except Exception:
        pass

    tab.stop()
    return full_scripts


def search_keywords(scripts: list[dict]) -> list[dict]:
    """Procura KEYWORDS no conteúdo de cada script."""
    hits = []
    for script in scripts:
        content = script.get("content", "")
        src = script.get("src", "?")
        for kw in KEYWORDS:
            if kw.lower() in content.lower():
                # Encontra todas as ocorrências com contexto
                for m in re.finditer(re.escape(kw), content, re.IGNORECASE):
                    start = max(0, m.start() - 150)
                    end = min(len(content), m.end() + 150)
                    context = content[start:end]
                    hits.append({
                        "keyword": kw,
                        "file": src,
                        "position": m.start(),
                        "context": context,
                        "line_before": content[max(0, start - 200):start],
                        "line_after": content[end:min(len(content), end + 200)],
                    })
    return hits


def main():
    if len(sys.argv) < 2:
        print("Uso: python diagnose_codes.py <porta> [fragmento_url]")
        print("  Ex: python diagnose_codes.py 9316 551476")
        sys.exit(1)

    port = int(sys.argv[1])
    fragment = sys.argv[2] if len(sys.argv) > 2 else "wbslot"

    tab_id = find_game_tab(port, fragment)
    if not tab_id:
        log.error(f"Nenhuma aba com '{fragment}' na URL encontrada na porta {port}")
        log.info("Abas disponíveis:")
        for t in list_tabs(port):
            log.info(f"  {t['id'][:20]}... | {t.get('title', '?')} | {t.get('url', '?')[:80]}")
        sys.exit(1)

    log.info(f"Aba do jogo encontrada: {tab_id[:30]}...")

    scripts = extract_scripts(port, tab_id)
    log.info(f"{len(scripts)} scripts extraídos")

    hits = search_keywords(scripts)
    if not hits:
        log.info("Nenhuma keyword encontrada nos scripts do jogo.")
        log.info("Dump dos nomes de script carregados:")
        for s in scripts:
            log.info(f"  {s['src']} ({s['size']} bytes)")
        return

    log.info(f"\n{'='*80}")
    log.info(f"  {len(hits)} hits encontrados em {len(set(h['file'] for h in hits))} arquivo(s)")
    log.info(f"{'='*80}\n")

    for i, hit in enumerate(hits):
        log.info(f"--- Hit #{i+1}: '{hit['keyword']}' em {hit['file']} (pos {hit['position']}) ---")
        log.info(f"  Contexto: ...{hit['context']}...")
        log.info("")

    # Salva resultado completo em JSON
    out = {
        "total_scripts": len(scripts),
        "total_hits": len(hits),
        "scripts": [{"src": s["src"], "size": s["size"]} for s in scripts],
        "hits": hits,
    }
    out_path = __import__("pathlib").Path(__file__).parent / "diagnose_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False, default=str)
    log.info(f"Resultado completo salvo em: {out_path}")


if __name__ == "__main__":
    main()

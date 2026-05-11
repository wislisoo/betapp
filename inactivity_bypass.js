(function () {
    // Skip em Workers ou contextos sem DOM
    if (typeof document === 'undefined' || typeof document.querySelector !== 'function') return;

    console.log("🚀 [Antigravity] Super Bypass Ativado.");

    var origSetTimeout  = window.setTimeout;
    var origSetInterval = window.setInterval;

    function isSuspect(fn) {
        if (!fn) return false;
        try {
            var src = typeof fn === 'function' ? fn.toString() : String(fn);
            return /kickout|closegametab|SetIdleKickOut|idle/i.test(src);
        } catch (e) {
            return false;
        }
    }

    window.setTimeout = function (fn, delay) {
        if (isSuspect(fn)) {
            console.warn('🚫 [TIMEOUT BLOQUEADO] Kick-out detectado.');
            return origSetTimeout(function(){}, 9999999);
        }
        return origSetTimeout.apply(this, arguments);
    };

    window.setInterval = function (fn, delay) {
        if (isSuspect(fn)) {
            console.warn('🚫 [INTERVAL BLOQUEADO] Monitor de inatividade bloqueado.');
            return origSetInterval(function(){}, 9999999);
        }
        return origSetInterval.apply(this, arguments);
    };

    // Mantém visibilidade sempre ativa
    function ensureVisible() {
        try {
            Object.defineProperty(document, 'visibilityState', { get: function(){return 'visible';}, configurable: true });
            Object.defineProperty(document, 'hidden',          { get: function(){return false;},    configurable: true });
        } catch(e) {}
    }
    ensureVisible();

    ['visibilitychange', 'webkitvisibilitychange', 'mozvisibilitychange', 'msvisibilitychange'].forEach(function(evt) {
        window.addEventListener(evt, function(e) { e.stopImmediatePropagation(); }, true);
    });

    // ── Reaplica após navegação SPA (pushState/replaceState/hashchange) ──
    window.addEventListener('popstate', ensureVisible);
    window.addEventListener('hashchange', ensureVisible);

    // MutationObserver: detecta troca grande de DOM (ex: router SPA)
    try {
        var _observer = new MutationObserver(function(muts) {
            var bigChange = false;
            for (var i = 0; i < muts.length; i++) {
                if (muts[i].addedNodes.length > 10) { bigChange = true; break; }
            }
            if (bigChange) ensureVisible();
        });
        _observer.observe(document.documentElement || document.body, { childList: true, subtree: true });
    } catch(e) {}

    // ── Keepalive ativo: ping a cada 45s para manter sessão viva no backend ──
    origSetInterval(function() {
        try { fetch('/gameList?flag=103', { method: 'HEAD', cache: 'no-cache' }); } catch(e) {}
    }, 45000);

    console.log('✅ Proteção completa aplicada com sucesso.');
})();

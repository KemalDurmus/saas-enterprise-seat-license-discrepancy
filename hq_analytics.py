"""HQ analitik katmanı — Geliştirici Ajan bu dosyaya dokunmaz.

- Sayfa görüntüleme + günlük tekil ziyaretçi (IP/UA özeti saklanır, ham IP saklanmaz)
- Bekleme listesi (/_hq/waitlist) ve geri bildirim (/_hq/feedback)
- Satın alma isteği (/_hq/buy): anında Telegram bildirimi; PAYMENT_URL ayarlıysa ödeme sayfasına yönlendirir
- Hata alarmı: uygulamada ERROR seviyesinde kaydedilen her istisna (yakalanmamış 500'ler dahil) anında Telegram'a
  gider (aynı hata 10 dakikada en fazla bir kez) ve /stats içinde raporlanır. /_hq/selftest-error (STATS_TOKEN ile)
  alarmı canlıda denemek için bilerek hata üretir. /_hq/buy-selftest (STATS_TOKEN ile) satın alma bildirimini
  istatistiğe yazmadan dener.
- /stats: STATS_TOKEN ile korunan özet; Agent HQ Analiz ekibi düzenli okur
- Yeni kayıt/geri bildirim olunca (HQ_TG_TOKEN + HQ_TG_CHAT varsa) Telegram bildirimi

Dil: app_meta.json içindeki "lang" ("tr" varsayılan, "en") şablonun sabit metinlerini belirler.

Not: Ücretsiz barındırmada disk geçicidir; uygulama yeniden başlayınca sayaçlar sıfırlanabilir.
Bu yüzden /stats bir boot_id döndürür ve HQ her açılışın verisini ayrı ayrı biriktirir.
"""
import hashlib
import logging
import os
import traceback
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import abort, flash, has_request_context, jsonify, redirect, request

BASE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("DATA_DIR", BASE / "data")) / "hq.sqlite3"
BOOT_ID = uuid.uuid4().hex[:12]
BOOT_AT = datetime.now(timezone.utc).isoformat(timespec="seconds")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[a-zA-Z]{2,24}$")
SKIP_PREFIXES = ("/static", "/health", "/stats", "/_hq", "/favicon")
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS views (ts TEXT, day TEXT, path TEXT, visitor TEXT);
        CREATE TABLE IF NOT EXISTS signups (ts TEXT, email TEXT UNIQUE, page TEXT);
        CREATE TABLE IF NOT EXISTS feedback (ts TEXT, text TEXT, page TEXT);
        CREATE TABLE IF NOT EXISTS events (ts TEXT, name TEXT, page TEXT);
        CREATE TABLE IF NOT EXISTS purchases (ts TEXT, email TEXT, page TEXT);
        CREATE TABLE IF NOT EXISTS errors (ts TEXT, path TEXT, kind TEXT, detail TEXT);
    """)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write(sql: str, args: tuple) -> bool:
    try:
        with _lock, _db() as conn:
            conn.execute(sql, args)
        return True
    except sqlite3.IntegrityError:
        return False
    except Exception:
        return False


def _visitor() -> str:
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
    ua = request.headers.get("User-Agent", "")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return hashlib.sha256(f"{ip}|{ua}|{day}".encode()).hexdigest()[:16]


def _notify(text: str, wait: bool = False) -> bool:
    """Telegram'a yazar. wait=True ise gönderimi bekler ve Telegram'ın kabul edip etmediğini döndürür."""
    token, chat = os.environ.get("HQ_TG_TOKEN"), os.environ.get("HQ_TG_CHAT")
    if not (token and chat):
        return False

    def send() -> bool:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": text[:3500]}).encode()
            with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10) as r:
                return r.status == 200
        except Exception:
            return False

    if wait:
        return send()
    threading.Thread(target=send, daemon=True).start()
    return True


def _meta() -> dict:
    try:
        import json
        return json.loads((BASE / "app_meta.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _app_title() -> str:
    return _meta().get("app_title", "App")


STRINGS = {
    "tr": {
        "buy_title": "Pro'ya geç", "buy_btn": "Satın almak istiyorum",
        "buy_text": "Hemen kullanmaya başlamak istiyorsan e-postanı bırak; ödeme ve kurulum bilgisini sana gönderelim.",
        "buy_thanks": "Teşekkürler! Ödeme ve kurulum bilgisini kısa süre içinde e-postana göndereceğiz. 🙌",
        "email_ph": "e-posta adresin", "bad_email": "Lütfen geçerli bir e-posta adresi gir.",
        "wl_title": "Yeni özelliklerden ilk sen haberdar ol", "wl_btn": "Listeye katıl",
        "wl_text": "Bekleme listesine katıl; ücretsiz erken erişim ve indirimler e-postana gelsin.",
        "wl_thanks": "Teşekkürler! Listeye eklendin, yenilikleri ilk sen duyacaksın. 🎉",
        "wl_dupe": "Bu e-posta zaten listede. 🙌",
        "fb_summary": "🛠️ Bir sorun mu var ya da bir şey mi eksik? Bize yaz", "fb_btn": "Gönder",
        "fb_ph": "Ne oldu? Hangi dosyayı yükledin, ne bekliyordun, ne gördün?",
        "fb_email_ph": "e-posta (isteğe bağlı — dönüş yapabilmemiz için)",
        "fb_short": "Geri bildirim çok kısa.", "fb_thanks": "Geri bildirimin için teşekkürler! 💬",
        "not_found": "Sayfa bulunamadı.", "too_large": "Dosya çok büyük (en fazla 10 MB).",
    },
    "en": {
        "buy_title": "Get Pro", "buy_btn": "I want to buy",
        "buy_text": "Want to start using it now? Leave your email and we'll send you payment and setup details.",
        "buy_thanks": "Thanks! We'll email you payment and setup details shortly. 🙌",
        "email_ph": "your email", "bad_email": "Please enter a valid email address.",
        "wl_title": "Be the first to hear about new features", "wl_btn": "Join the waitlist",
        "wl_text": "Join the waitlist for free early access and discounts.",
        "wl_thanks": "Thanks! You're on the list — you'll hear about updates first. 🎉",
        "wl_dupe": "This email is already on the list. 🙌",
        "fb_summary": "🛠️ Something wrong or missing? Tell us", "fb_btn": "Send",
        "fb_ph": "What happened? What did you upload, what did you expect, what did you see?",
        "fb_email_ph": "email (optional — so we can get back to you)",
        "fb_short": "Feedback is too short.", "fb_thanks": "Thanks for your feedback! 💬",
        "not_found": "Page not found.", "too_large": "File too large (max 10 MB).",
    },
}


def lang() -> str:
    return _meta().get("lang", "tr") if _meta().get("lang") in STRINGS else "tr"


def t(key: str) -> str:
    """Şablonun sabit metinleri, uygulamanın diline göre."""
    return STRINGS[lang()].get(key, STRINGS["tr"][key])


_last_alert: dict[str, float] = {}


def report_error(kind: str, detail: str) -> None:
    """Hata kaydı + anında Telegram alarmı. Aynı hata (tür + ilk satır) 10 dakikada en fazla bir kez bildirilir."""
    path = request.path if has_request_context() else "-"
    _write("INSERT INTO errors(ts, path, kind, detail) VALUES (?,?,?,?)", (_now(), path[:200], kind[:120], detail[:2000]))
    key = f"{path}|{kind}|{detail.splitlines()[0][:120] if detail else ''}"
    now = time.time()
    if now - _last_alert.get(key, 0) < 600:
        return
    _last_alert[key] = now
    _notify(f"🔴 HATA — {_app_title()}\nSayfa: {path}\n{kind}: {detail[:600]}")


class _ErrorAlarm(logging.Handler):
    """ERROR seviyesindeki istisna kayıtlarını (Flask'ın 500 kaydı ve feature.py'nin logging.exception'ı) yakalar."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                tb = traceback.extract_tb(record.exc_info[2])
                where = f" ({tb[-1].filename.split('/')[-1].split(chr(92))[-1]}:{tb[-1].lineno})" if tb else ""
                report_error(type(exc).__name__, f"{exc}{where}")
            elif record.levelno >= logging.ERROR:
                report_error("Error", record.getMessage())
        except Exception:
            pass  # alarm hiçbir zaman uygulamayı düşürmesin


def init_app(app) -> None:
    root = logging.getLogger()  # app.logger ve feature.py kayıtları buraya akar; tek yerde dinle (çift kayıt olmasın)
    if not any(isinstance(h, _ErrorAlarm) for h in root.handlers):
        root.addHandler(_ErrorAlarm(level=logging.ERROR))

    app.jinja_env.globals["hq_price"] = os.environ.get("PRICE_TEXT", "")
    app.jinja_env.globals["hq_t"] = t
    app.jinja_env.globals["hq_lang"] = lang

    @app.before_request
    def _track():
        if request.method != "GET" or request.path.startswith(SKIP_PREFIXES):
            return
        if "bot" in request.headers.get("User-Agent", "").lower():
            return
        now = _now()
        _write("INSERT INTO views(ts, day, path, visitor) VALUES (?,?,?,?)",
               (now, now[:10], request.path[:200], _visitor()))

    @app.post("/_hq/waitlist")
    def _waitlist():
        email = (request.form.get("email") or "").strip().lower()[:254]
        if not EMAIL_RE.match(email):
            flash(t("bad_email"), "error")
        elif _write("INSERT INTO signups(ts, email, page) VALUES (?,?,?)",
                    (_now(), email, (request.referrer or "")[:200])):
            flash(t("wl_thanks"), "success")
            _notify(f"🎉 {_app_title()}: yeni bekleme listesi kaydı → {email}")
        else:
            flash(t("wl_dupe"), "success")
        return redirect(request.referrer or "/")

    @app.post("/_hq/buy")
    def _buy():
        email = (request.form.get("email") or "").strip().lower()[:254]
        if not EMAIL_RE.match(email):
            flash(t("bad_email"), "error")
            return redirect(request.referrer or "/")
        _write("INSERT INTO purchases(ts, email, page) VALUES (?,?,?)", (_now(), email, (request.referrer or "")[:200]))
        _notify(f"💰 SATIN ALMA İSTEĞİ — {_app_title()}\n{email}\nBu kişi ürünü almak istiyor; hemen ödeme bilgisiyle cevap ver.")
        pay = os.environ.get("PAYMENT_URL")
        if pay:
            return redirect(pay)
        flash(t("buy_thanks"), "success")
        return redirect(request.referrer or "/")

    @app.post("/_hq/feedback")
    def _feedback():
        text = (request.form.get("text") or "").strip()[:2000]
        email = (request.form.get("email") or "").strip().lower()[:254]
        if email and not EMAIL_RE.match(email):
            email = ""
        if len(text) < 3:
            flash(t("fb_short"), "error")
        else:
            stored = f"[{email}] {text}" if email else text
            _write("INSERT INTO feedback(ts, text, page) VALUES (?,?,?)", (_now(), stored, (request.referrer or "")[:200]))
            flash(t("fb_thanks"), "success")
            _notify(f"🛠️ {_app_title()} — kullanıcı bildirimi" + (f" ({email}, cevap bekliyor)" if email else "")
                    + f"\nSayfa: {request.referrer or '-'}\n{text[:800]}")
        return redirect(request.referrer or "/")

    @app.post("/_hq/selftest-error")
    def _selftest_error():
        """Alarm zincirini canlıda denemek için: yalnızca STATS_TOKEN ile, bilerek 500 üretir."""
        token = os.environ.get("STATS_TOKEN")
        if not token or request.headers.get("Authorization") != f"Bearer {token}":
            abort(404)
        raise RuntimeError("TEST — alarm denemesi (bilerek üretilen hata, müşteri etkilenmedi)")

    @app.post("/_hq/buy-selftest")
    def _buy_selftest():
        """Satın alma bildirimini müşteri olmadan dener: yalnızca STATS_TOKEN ile. Satın alma olarak KAYDEDİLMEZ
        (istatistik bozulmaz); gerçek bildirimle aynı yoldan 'TEST' etiketli bir Telegram mesajı gider."""
        token = os.environ.get("STATS_TOKEN")
        if not token or request.headers.get("Authorization") != f"Bearer {token}":
            abort(404)
        sent = _notify(f"🧪 TEST — satın alma bildirimi denemesi ({_app_title()}). Gerçek müşteri değil, "
                       "işlem gerekmez.", wait=True)
        return jsonify(telegram=sent, payment_url=bool(os.environ.get("PAYMENT_URL")))

    @app.post("/_hq/event")
    def _event():
        payload = (request.get_json(silent=True) or {}) if request.is_json else request.form
        name = str(payload.get("name") or "")
        if name:
            _write("INSERT INTO events(ts, name, page) VALUES (?,?,?)", (_now(), name[:80], request.path))
        return ("", 204)

    @app.get("/stats")
    def _stats():
        token = os.environ.get("STATS_TOKEN")
        if not token or request.headers.get("Authorization") != f"Bearer {token}":
            abort(404)
        with _lock, _db() as conn:
            q = lambda sql, *a: conn.execute(sql, a).fetchall()
            days = q("SELECT day, COUNT(*), COUNT(DISTINCT visitor) FROM views GROUP BY day ORDER BY day")
            return jsonify(
                boot_id=BOOT_ID, boot_at=BOOT_AT, generated_at=_now(),
                views=q("SELECT COUNT(*) FROM views")[0][0],
                uniques=sum(d[2] for d in days),
                by_day=[{"day": d[0], "views": d[1], "uniques": d[2]} for d in days],
                top_paths=[{"path": p, "views": n} for p, n in
                           q("SELECT path, COUNT(*) n FROM views GROUP BY path ORDER BY n DESC LIMIT 10")],
                signups=[{"ts": t, "email": e} for t, e in q("SELECT ts, email FROM signups ORDER BY ts")],
                feedback=[{"ts": t, "text": x} for t, x in q("SELECT ts, text FROM feedback ORDER BY ts")],
                purchases=[{"ts": t, "email": e} for t, e in q("SELECT ts, email FROM purchases ORDER BY ts")],
                errors=[{"ts": t, "path": p, "kind": k, "detail": d} for t, p, k, d in
                        q("SELECT ts, path, kind, detail FROM errors ORDER BY ts DESC LIMIT 50")],
                events=[{"name": n, "count": c} for n, c in q("SELECT name, COUNT(*) FROM events GROUP BY name")],
            )

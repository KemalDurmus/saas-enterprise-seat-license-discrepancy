"""Duman testi — Geliştirici Ajan her derlemeden sonra bunu çalıştırır.

1. Uygulama import edilip oluşturulabiliyor mu?
2. Parametresiz tüm GET rotaları 5xx vermeden açılıyor mu?
3. sample_requests.json içindeki örnek akışlar 5xx vermeden çalışıyor mu?
4. Ekranda yarım/bozuk metin var mı? ("account(s)", "None", "nan", ham Jinja, TODO, lorem ipsum)
5. Ziyaretçiler arası veri sızıntısı: bir ziyaretçinin örnek akışları, BAŞKA bir ziyaretçinin gördüğü
   sayfaları değiştiriyor mu? (uygulama kullanıcı verisini ortak bir yerde saklıyorsa değiştirir)
Hata olursa traceback stderr'e basılır ve çıkış kodu 1 olur.
"""
import html
import json
import re
import sys
import traceback
from pathlib import Path

BASE = Path(__file__).resolve().parent
BROKEN_TEXT = [
    (re.compile(r"[A-Za-zçğıöşü]\((s|es|ler|lar)\)"), "yarım çoğul eki"),
    (re.compile(r"(?<![\w-])(None|nan|NaN|undefined|null)(?![\w-])"), "ham boş değer"),
    (re.compile(r"\{\{|\{%|%\}|\}\}"), "işlenmemiş şablon kodu"),
    (re.compile(r"(?i)\bTODO\b|lorem ipsum|burayı doldur"), "yer tutucu metin"),
]


def visible_text(page: bytes) -> str:
    s = page.decode("utf-8", "replace")
    s = re.sub(r"(?is)<(script|style|textarea)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    return " ".join(html.unescape(s).split())


def text_problems(page: bytes) -> list[str]:
    text = visible_text(page)
    out = []
    for rx, label in BROKEN_TEXT:
        m = rx.search(text)
        if m:
            out.append(f"{label}: …{text[max(0, m.start() - 30):m.end() + 30]}…")
    return out


def main() -> int:
    from app import create_app

    app = create_app()
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    client = app.test_client()
    failures = 0

    def check(method: str, path: str, data: dict | None = None, strict: bool = False) -> None:
        """strict=True (örnek akışlar): yönlendirmeler izlenir; 4xx/5xx ya da sayfadaki hata uyarısı başarısızlıktır."""
        nonlocal failures
        try:
            resp = client.open(path, method=method, data=data or None, follow_redirects=strict)
        except Exception:
            failures += 1
            print(f"FAIL {method} {path} — istisna fırlattı:", file=sys.stderr)
            traceback.print_exc()
            return
        limit = 400 if strict else 500
        error_shown = strict and b"alert alert-error" in resp.data
        problems = text_problems(resp.data) if resp.status_code < 400 else []
        if problems:
            failures += 1
            print(f"FAIL {method} {path} ekranda bozuk metin: " + " | ".join(problems), file=sys.stderr)
            return
        if resp.status_code >= limit or error_shown:
            failures += 1
            why = "sayfada hata uyarısı gösterildi" if error_shown else f"-> {resp.status_code}"
            hint = " (yanlış yol? rotalar: " + ", ".join(sorted({r.rule for r in app.url_map.iter_rules()
                                                                 if r.endpoint != 'static'})) + ")" \
                if resp.status_code == 404 else ""
            print(f"FAIL {method} {path} {why}{hint}", file=sys.stderr)
        else:
            print(f"ok   {method} {path} -> {resp.status_code}")

    pages = [r.rule for r in app.url_map.iter_rules()
             if "GET" in r.methods and not r.arguments and r.endpoint != "static"
             and not r.rule.startswith(("/stats", "/_hq", "/health"))]
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and not rule.arguments and rule.endpoint != "static":
            check("GET", rule.rule)

    # Başka bir ziyaretçinin gördüğü sayfalar (rakamlar normalize: saat/tarih farkı sızıntı sayılmaz)
    observer = app.test_client()
    snapshot = lambda: {p: re.sub(r"\d", "#", visible_text(observer.get(p).data)) for p in pages}
    before = snapshot()

    samples_path = BASE / "sample_requests.json"
    if samples_path.exists():
        for s in json.loads(samples_path.read_text(encoding="utf-8")):
            data = {f["name"]: f["value"] for f in s.get("form_fields", [])}
            check(s["method"].upper(), s["path"], data, strict=True)
            # Satır sonları: Windows (\r\n) ve eski Mac (\r) dosyaları da aynı sonucu vermeli. CSV'yi
            # csv.reader(io.StringIO(text)) ile okumadan önce text.replace("\r\n", "\n").replace("\r", "\n") yap.
            if any("\n" in str(v) for v in data.values()):
                for eol, label in (("\r\n", "\\r\\n"), ("\r", "\\r")):
                    before_fail = failures
                    check(s["method"].upper(), s["path"],
                          {k: str(v).replace("\n", eol) if "\n" in str(v) else v for k, v in data.items()}, strict=True)
                    if failures > before_fail:
                        print(f"FAIL satır sonu {label}: aynı örnek veri {label} satır sonlarıyla gelince çalışmıyor. "
                              "CSV'yi okumadan önce text = text.replace('\\r\\n', '\\n').replace('\\r', '\\n') yap.",
                              file=sys.stderr)
                    else:
                        print(f"ok   satır sonu {label} -> {s['path']}")

    # Büyük girdi: gerçek kullanıcılar binlerce satır yapıştırır. En uzun metin alanını ~1,5 MB'a büyüt;
    # uygulama kendi sınırı için uyarı gösterebilir ama 413/5xx ya da genel "analiz edilemedi" hatası vermemeli.
    if samples_path.exists():
        posts = [s for s in json.loads(samples_path.read_text(encoding="utf-8")) if s["method"].upper() == "POST"]
        cand = [(s, f) for s in posts for f in s.get("form_fields", []) if "\n" in str(f["value"])]
        if cand:
            s, field = max(cand, key=lambda x: len(str(x[1]["value"])))
            head, _, body = str(field["value"]).partition("\n")
            big = head + "\n" + (body.rstrip("\n") + "\n") * max(1, 1_500_000 // max(1, len(body)))
            data = {f["name"]: (big if f is field else f["value"]) for f in s.get("form_fields", [])}
            resp = app.test_client().open(s["path"], method="POST", data=data, follow_redirects=True)
            generic = re.search(rb"(?i)could not be (analy|process)|bir hata olu|beklenmeyen", resp.data)
            if resp.status_code >= 400 or generic:
                failures += 1
                print(f"FAIL büyük girdi ({len(big) // 1024} KB yapıştırma) -> {resp.status_code}"
                      f"{' genel hata mesajı' if generic else ''}: uygulama büyük girdiyi işleyemiyor", file=sys.stderr)
            else:
                print(f"ok   büyük girdi ({len(big) // 1024} KB) -> {resp.status_code}")

        # Bozuk girdi: ikili dosya içeriği (NUL, \r, kontrol karakterleri) yapıştırılınca 5xx olmamalı
        if cand:
            junk = "".join(map(chr, range(256))) * 20
            data = {f["name"]: (junk if f is field else f["value"]) for f in s.get("form_fields", [])}
            try:
                resp = app.test_client().open(s["path"], method="POST", data=data, follow_redirects=True)
                status, crash = resp.status_code, ""
            except Exception as e:  # TESTING modunda istisna doğrudan buraya gelir
                status, crash = 500, f"{type(e).__name__}: {e}"
            if status >= 500:
                failures += 1
                print(f"FAIL bozuk/ikili girdi -> {status} {crash[:200]}: yanlış dosyada çökmemeli, kullanıcıya "
                      "'bu dosya CSV/metin değil' gibi açık bir mesaj göstermeli", file=sys.stderr)
            else:
                print(f"ok   bozuk/ikili girdi -> {status}")

    after = snapshot()
    leaked = [p for p in pages if before[p] != after[p]]
    if leaked:
        failures += 1
        print("FAIL veri sızıntısı: bir ziyaretçinin örnek akışları başka bir ziyaretçinin gördüğü şu sayfaları "
              f"değiştirdi: {', '.join(leaked)}. Kullanıcı verisini sunucuda ortak bir yerde saklama; durumsuz tasarla.",
              file=sys.stderr)
    else:
        print(f"ok   veri sızıntısı yok ({len(pages)} sayfa)")

    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)

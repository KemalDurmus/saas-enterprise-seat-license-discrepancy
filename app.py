"""Flask şablonu — Geliştirici Ajan bu dosyaya dokunmaz.

Uygulamaya özel her şey `feature.py` içindeki `bp` Blueprint'inde yaşar.
"""
import json
import os
from pathlib import Path

from flask import Flask, jsonify, render_template

BASE_DIR = Path(__file__).resolve().parent


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(24).hex())
    app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB yükleme sınırı
    # Werkzeug yapıştırılan metin alanlarını varsayılan olarak 500 KB ile sınırlar; büyük CSV/log yapıştırınca
    # 413 verirdi. Dosya yüklemeyle aynı sınıra çekilir.
    app.config["MAX_FORM_MEMORY_SIZE"] = app.config["MAX_CONTENT_LENGTH"]

    meta_path = BASE_DIR / "app_meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}

    @app.context_processor
    def inject_meta():
        return {"app_title": meta.get("app_title", "App"), "tagline": meta.get("tagline", "")}

    from hq_analytics import init_app as init_analytics, t
    init_analytics(app)

    from feature import bp
    app.register_blueprint(bp)

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.errorhandler(404)
    def not_found(_):
        return render_template("base.html", error=t("not_found")), 404

    @app.errorhandler(413)
    def too_large(_):
        return render_template("base.html", error=t("too_large")), 413

    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    create_app().run(host="127.0.0.1", port=port, debug=False)

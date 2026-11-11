from urllib.parse import quote

from flask import Blueprint, flash, render_template, request

import logic

bp = Blueprint("feature", __name__)


def _read_input(file_key, text_key, label):
    """Read an uploaded file or pasted text into (text, notes) (in memory only)."""
    f = request.files.get(file_key)
    if f is not None and f.filename:
        data = f.stream.read(logic.MAX_BYTES + 1)
        return logic.decode_upload(data, label)
    pasted = (request.form.get(text_key) or "").strip()
    if pasted:
        return logic.decode_upload(pasted.encode("utf-8"), label)
    raise ValueError(f"Please upload the {label} or paste its contents.")


@bp.route("/")
def index():
    return render_template("index.html", result=None, form={})


@bp.route("/analyze", methods=["POST"])
def analyze():
    form = {
        "price": (request.form.get("price") or "").strip(),
        "stripe_text": request.form.get("stripe_text") or "",
        "users_text": request.form.get("users_text") or "",
    }
    try:
        upload_notes = []
        if request.form.get("demo"):
            stripe_text, users_text = logic.demo_data()
            demo = True
        else:
            demo = False
            stripe_text, n1 = _read_input("stripe_file", "stripe_text", "Stripe file")
            users_text, n2 = _read_input("users_file", "users_text", "user file")
            upload_notes = list(n1) + list(n2)
        result = logic.run(stripe_text, users_text, form["price"])
        result["notes"] = upload_notes + result["notes"]
        result["demo"] = demo
        result["data_uri"] = "data:text/csv;charset=utf-8," + quote(result["csv"])
        return render_template("index.html", result=result, form=form)
    except ValueError as exc:
        flash(str(exc), "error")
    except Exception:
        flash("Something went wrong while reading your files. Please check that both are CSV exports and try again.", "error")
    return render_template("index.html", result=None, form=form)

import os
from datetime import datetime, date, time, timedelta

from functools import wraps

from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///reservierungen.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.urandom(24)

db = SQLAlchemy(app)

# Admin-Passwort (beim ersten Start ändern!)
ADMIN_PASSWORT_HASH = generate_password_hash(os.environ.get("ADMIN_PASSWORT", "admin123"))

SLOT_DURATION_HOURS = 2


def admin_erforderlich(f):
    """Decorator: Prüft ob der Admin eingeloggt ist."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_eingeloggt"):
            if request.is_json or request.path.startswith("/api/"):
                return jsonify({"error": "Nicht autorisiert"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated

# --- Datenmodell ---

WEEKDAY_NAMES = {
    0: "Montag",
    1: "Dienstag",
    2: "Mittwoch",
    3: "Donnerstag",
    4: "Freitag",
    5: "Samstag",
    6: "Sonntag",
}


class Tisch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nummer = db.Column(db.Integer, unique=True, nullable=False)
    plaetze = db.Column(db.Integer, nullable=False)
    reservierungen = db.relationship("Reservierung", backref="tisch", lazy=True)


class Oeffnungszeit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    wochentag = db.Column(db.Integer, nullable=False)  # 0=Mo ... 6=So
    oeffnung = db.Column(db.Time, nullable=False)
    schliessung = db.Column(db.Time, nullable=False)
    geschlossen = db.Column(db.Boolean, default=False)


class Reservierung(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tisch_id = db.Column(db.Integer, db.ForeignKey("tisch.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    telefon = db.Column(db.String(30), nullable=False)
    email = db.Column(db.String(120), nullable=False, default="")
    gaeste = db.Column(db.Integer, nullable=False)
    datum = db.Column(db.Date, nullable=False)
    uhrzeit = db.Column(db.Time, nullable=False)
    erstellt_am = db.Column(db.DateTime, default=datetime.utcnow)


# --- Hilfsfunktionen ---


def get_oeffnungszeit(wochentag: int) -> Oeffnungszeit | None:
    return Oeffnungszeit.query.filter_by(wochentag=wochentag).first()


def verfuegbare_slots(tag: date) -> list[time]:
    """Gibt die buchbaren Startzeiten für einen Tag zurück."""
    oz = get_oeffnungszeit(tag.weekday())
    if oz is None or oz.geschlossen:
        return []

    slots = []
    current = datetime.combine(tag, oz.oeffnung)
    end = datetime.combine(tag, oz.schliessung)

    while current + timedelta(hours=SLOT_DURATION_HOURS) <= end:
        slots.append(current.time())
        current += timedelta(hours=1)  # Slots im Stundentakt

    return slots


def finde_tisch(gaeste: int, tag: date, slot: time) -> Tisch | None:
    """Findet den kleinsten freien Tisch, der für die Gästeanzahl reicht."""
    slot_start = datetime.combine(tag, slot)
    slot_end = slot_start + timedelta(hours=SLOT_DURATION_HOURS)

    tische = Tisch.query.filter(Tisch.plaetze >= gaeste).order_by(Tisch.plaetze).all()

    for tisch in tische:
        konflikt = Reservierung.query.filter(
            Reservierung.tisch_id == tisch.id,
            Reservierung.datum == tag,
        ).all()

        belegt = False
        for res in konflikt:
            res_start = datetime.combine(tag, res.uhrzeit)
            res_end = res_start + timedelta(hours=SLOT_DURATION_HOURS)
            if slot_start < res_end and slot_end > res_start:
                belegt = True
                break

        if not belegt:
            return tisch

    return None


# --- Routen: Gäste-Frontend ---


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/slots", methods=["GET"])
def api_slots():
    """Gibt verfügbare Zeitslots für ein Datum zurück."""
    datum_str = request.args.get("datum")
    if not datum_str:
        return jsonify({"error": "Datum fehlt"}), 400

    try:
        tag = date.fromisoformat(datum_str)
    except ValueError:
        return jsonify({"error": "Ungültiges Datum"}), 400

    if tag < date.today():
        return jsonify({"error": "Datum liegt in der Vergangenheit"}), 400

    oz = get_oeffnungszeit(tag.weekday())
    if oz is None or oz.geschlossen:
        wochentag_name = WEEKDAY_NAMES.get(tag.weekday(), "")
        return jsonify({"slots": [], "hinweis": f"Am {wochentag_name} ist geschlossen."})

    slots = verfuegbare_slots(tag)
    return jsonify({"slots": [s.strftime("%H:%M") for s in slots]})


@app.route("/api/verfuegbarkeit", methods=["GET"])
def api_verfuegbarkeit():
    """Prüft ob für Gästeanzahl + Datum + Uhrzeit ein Tisch frei ist."""
    datum_str = request.args.get("datum")
    zeit_str = request.args.get("uhrzeit")
    gaeste = request.args.get("gaeste", type=int)

    if not all([datum_str, zeit_str, gaeste]):
        return jsonify({"error": "Parameter fehlen"}), 400

    try:
        tag = date.fromisoformat(datum_str)
        slot = time.fromisoformat(zeit_str)
    except ValueError:
        return jsonify({"error": "Ungültiges Format"}), 400

    tisch = finde_tisch(gaeste, tag, slot)
    if tisch:
        return jsonify({"verfuegbar": True, "tisch_nummer": tisch.nummer, "plaetze": tisch.plaetze})
    else:
        return jsonify({"verfuegbar": False, "meldung": "Leider ist zu dieser Zeit kein passender Tisch verfügbar."})


@app.route("/api/reservieren", methods=["POST"])
def api_reservieren():
    """Erstellt eine neue Reservierung."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Keine Daten"}), 400

    required = ["name", "telefon", "email", "gaeste", "datum", "uhrzeit"]
    for field in required:
        if field not in data or not data[field]:
            return jsonify({"error": f"Feld '{field}' fehlt"}), 400

    try:
        tag = date.fromisoformat(data["datum"])
        slot = time.fromisoformat(data["uhrzeit"])
        gaeste = int(data["gaeste"])
    except (ValueError, TypeError):
        return jsonify({"error": "Ungültiges Format"}), 400

    if tag < date.today():
        return jsonify({"error": "Datum liegt in der Vergangenheit"}), 400

    if gaeste < 1:
        return jsonify({"error": "Mindestens 1 Gast erforderlich"}), 400

    # Prüfe Öffnungszeiten
    oz = get_oeffnungszeit(tag.weekday())
    if oz is None or oz.geschlossen:
        return jsonify({"error": "An diesem Tag ist geschlossen."}), 400

    slots = verfuegbare_slots(tag)
    if slot not in slots:
        return jsonify({"error": "Diese Uhrzeit ist nicht buchbar."}), 400

    # Finde freien Tisch
    tisch = finde_tisch(gaeste, tag, slot)
    if tisch is None:
        return jsonify({"error": "Kein passender Tisch verfügbar."}), 409

    reservierung = Reservierung(
        tisch_id=tisch.id,
        name=data["name"].strip(),
        telefon=data["telefon"].strip(),
        email=data["email"].strip(),
        gaeste=gaeste,
        datum=tag,
        uhrzeit=slot,
    )
    db.session.add(reservierung)
    db.session.commit()

    return jsonify({
        "erfolg": True,
        "meldung": f"Reservierung bestätigt! Tisch {tisch.nummer} ({tisch.plaetze} Plätze) am {tag.strftime('%d.%m.%Y')} um {slot.strftime('%H:%M')} Uhr.",
        "reservierung_id": reservierung.id,
    })


# --- Routen: Admin ---


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_eingeloggt"):
        return redirect(url_for("admin"))

    fehler = None
    if request.method == "POST":
        passwort = request.form.get("passwort", "")
        if check_password_hash(ADMIN_PASSWORT_HASH, passwort):
            session["admin_eingeloggt"] = True
            return redirect(url_for("admin"))
        else:
            fehler = "Falsches Passwort."

    return render_template("admin_login.html", fehler=fehler)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_eingeloggt", None)
    return redirect(url_for("index"))


@app.route("/admin")
@admin_erforderlich
def admin():
    return render_template("admin.html")


@app.route("/api/admin/oeffnungszeiten", methods=["GET"])
@admin_erforderlich
def api_oeffnungszeiten():
    alle = Oeffnungszeit.query.order_by(Oeffnungszeit.wochentag).all()
    return jsonify([
        {
            "id": o.id,
            "wochentag": o.wochentag,
            "wochentag_name": WEEKDAY_NAMES.get(o.wochentag, ""),
            "oeffnung": o.oeffnung.strftime("%H:%M"),
            "schliessung": o.schliessung.strftime("%H:%M"),
            "geschlossen": o.geschlossen,
        }
        for o in alle
    ])


@app.route("/api/admin/oeffnungszeiten/<int:oz_id>", methods=["PUT"])
@admin_erforderlich
def api_oeffnungszeit_update(oz_id):
    oz = Oeffnungszeit.query.get_or_404(oz_id)
    data = request.get_json()

    if "oeffnung" in data:
        oz.oeffnung = time.fromisoformat(data["oeffnung"])
    if "schliessung" in data:
        oz.schliessung = time.fromisoformat(data["schliessung"])
    if "geschlossen" in data:
        oz.geschlossen = bool(data["geschlossen"])

    db.session.commit()
    return jsonify({"erfolg": True})


@app.route("/api/admin/reservierungen", methods=["GET"])
@admin_erforderlich
def api_admin_reservierungen():
    datum_str = request.args.get("datum")
    query = Reservierung.query

    if datum_str:
        try:
            tag = date.fromisoformat(datum_str)
            query = query.filter(Reservierung.datum == tag)
        except ValueError:
            pass

    reservierungen = query.order_by(Reservierung.datum, Reservierung.uhrzeit).all()
    return jsonify([
        {
            "id": r.id,
            "tisch_nummer": r.tisch.nummer,
            "tisch_plaetze": r.tisch.plaetze,
            "name": r.name,
            "telefon": r.telefon,
            "email": r.email,
            "gaeste": r.gaeste,
            "datum": r.datum.strftime("%d.%m.%Y"),
            "uhrzeit": r.uhrzeit.strftime("%H:%M"),
        }
        for r in reservierungen
    ])


@app.route("/api/admin/reservierungen/<int:res_id>", methods=["DELETE"])
@admin_erforderlich
def api_admin_reservierung_loeschen(res_id):
    res = Reservierung.query.get_or_404(res_id)
    db.session.delete(res)
    db.session.commit()
    return jsonify({"erfolg": True})


@app.route("/api/admin/tische", methods=["GET"])
@admin_erforderlich
def api_admin_tische():
    tische = Tisch.query.order_by(Tisch.nummer).all()
    return jsonify([
        {"id": t.id, "nummer": t.nummer, "plaetze": t.plaetze}
        for t in tische
    ])


@app.route("/api/admin/tische", methods=["POST"])
@admin_erforderlich
def api_admin_tisch_erstellen():
    """Erstellt einen neuen Tisch."""
    data = request.get_json()
    if not data:
        return jsonify({"error": "Keine Daten"}), 400

    nummer = data.get("nummer", type=None)
    plaetze = data.get("plaetze", type=None)

    if nummer is None or plaetze is None:
        return jsonify({"error": "Nummer und Plätze sind erforderlich"}), 400

    try:
        nummer = int(nummer)
        plaetze = int(plaetze)
    except (ValueError, TypeError):
        return jsonify({"error": "Ungültige Werte"}), 400

    if nummer < 1 or plaetze < 1:
        return jsonify({"error": "Nummer und Plätze müssen mindestens 1 sein"}), 400

    if Tisch.query.filter_by(nummer=nummer).first():
        return jsonify({"error": f"Tisch {nummer} existiert bereits"}), 409

    tisch = Tisch(nummer=nummer, plaetze=plaetze)
    db.session.add(tisch)
    db.session.commit()
    return jsonify({"erfolg": True, "id": tisch.id})


@app.route("/api/admin/tische/<int:tisch_id>", methods=["PUT"])
@admin_erforderlich
def api_admin_tisch_update(tisch_id):
    """Aktualisiert einen Tisch (Nummer und/oder Plätze)."""
    tisch = Tisch.query.get_or_404(tisch_id)
    data = request.get_json()

    if "nummer" in data:
        neue_nummer = int(data["nummer"])
        bestehend = Tisch.query.filter(Tisch.nummer == neue_nummer, Tisch.id != tisch_id).first()
        if bestehend:
            return jsonify({"error": f"Tisch {neue_nummer} existiert bereits"}), 409
        tisch.nummer = neue_nummer

    if "plaetze" in data:
        tisch.plaetze = int(data["plaetze"])

    db.session.commit()
    return jsonify({"erfolg": True})


@app.route("/api/admin/tische/<int:tisch_id>", methods=["DELETE"])
@admin_erforderlich
def api_admin_tisch_loeschen(tisch_id):
    """Löscht einen Tisch (nur wenn keine Reservierungen vorhanden)."""
    tisch = Tisch.query.get_or_404(tisch_id)

    offene = Reservierung.query.filter(
        Reservierung.tisch_id == tisch_id,
        Reservierung.datum >= date.today(),
    ).count()

    if offene > 0:
        return jsonify({"error": f"Tisch hat noch {offene} zukünftige Reservierung(en). Bitte erst stornieren."}), 409

    db.session.delete(tisch)
    db.session.commit()
    return jsonify({"erfolg": True})


# --- Datenbank initialisieren ---


def init_db():
    db.create_all()

    # Tische anlegen falls leer
    if Tisch.query.count() == 0:
        tisch_config = [
            (1, 2), (2, 2), (3, 2),    # 3x 2er-Tisch
            (4, 4), (5, 4), (6, 4),    # 3x 4er-Tisch
            (7, 6), (8, 6),            # 2x 6er-Tisch
            (9, 8),                     # 1x 8er-Tisch
            (10, 10),                   # 1x 10er-Tisch
        ]
        for nummer, plaetze in tisch_config:
            db.session.add(Tisch(nummer=nummer, plaetze=plaetze))

    # Öffnungszeiten anlegen falls leer
    if Oeffnungszeit.query.count() == 0:
        for wochentag in range(7):
            if wochentag in (0, 1):  # Mo+Di geschlossen (typisch für Bars)
                db.session.add(Oeffnungszeit(
                    wochentag=wochentag,
                    oeffnung=time(18, 0),
                    schliessung=time(23, 0),
                    geschlossen=True,
                ))
            else:
                db.session.add(Oeffnungszeit(
                    wochentag=wochentag,
                    oeffnung=time(18, 0),
                    schliessung=time(1, 0) if wochentag >= 4 else time(23, 0),
                    geschlossen=False,
                ))

    db.session.commit()


with app.app_context():
    init_db()

if __name__ == "__main__":
    app.run(debug=True, port=8080)

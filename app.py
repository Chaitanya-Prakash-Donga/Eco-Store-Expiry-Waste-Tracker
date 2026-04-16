import sqlite3
import smtplib
import bcrypt
import os  # Added to read Render Environment Variables
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import Flask, request, jsonify, g, render_template
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)
from apscheduler.schedulers.background import BackgroundScheduler

# ── CONFIGURATION (Updated for Render) ───────────────────────────────────────
class Config:
    # Use environment variables for security. If not set, it uses 'dev-secret'
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "eco-tracker-dev-secret")
    SESSION_HOURS = 24
    DATABASE = "eco_tracker.db"
    
    # These must be set in the 'Environment' tab on Render
    EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
    
    SMTP_HOST      = "smtp.gmail.com"
    SMTP_PORT      = 587
    LOW_STOCK_DEFAULT_MIN = 10

# ── DATABASE HELPERS ──────────────────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(Config.DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def raw_db():
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = raw_db()
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            store_name TEXT NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'manager',
            joined_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT NOT NULL,
            name TEXT NOT NULL,
            batch TEXT,
            cat TEXT DEFAULT 'Other',
            qty INTEGER NOT NULL DEFAULT 0,
            min_qty INTEGER DEFAULT 0,
            exp TEXT,
            price REAL DEFAULT 0.0,
            loc TEXT,
            added_by INTEGER REFERENCES users(id),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_name TEXT NOT NULL,
            product_id INTEGER REFERENCES products(id),
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            msg TEXT,
            sent_to TEXT,
            logged_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

# ── EMAIL & ALERTS ────────────────────────────────────────────────────────────
ALERT_STYLES = {
    "Expired": ("#e74c3c", "🚨"),
    "Expiry 48h": ("#e74c3c", "⏰"),
    "Expiry 7d": ("#f39c12", "📅"),
    "Out of Stock": ("#e74c3c", "❌"),
    "Low Stock": ("#9b59b6", "📦"),
}

def send_email(to_address, product_name, alert_type, message):
    if not Config.EMAIL_SENDER or not Config.EMAIL_PASSWORD:
        print("Skipping email: No credentials set in Environment Variables")
        return False

    color, icon = ALERT_STYLES.get(alert_type, ("#27ae60", "ℹ️"))
    subject = f"[Eco-Tracker] {alert_type} — {product_name}"
    html = f"""
    <html><body style="margin:0;padding:0;background:#f0f4f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center" style="padding:32px 12px;">
        <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;box-shadow:0 4px 18px rgba(0,0,0,0.1);overflow:hidden;">
          <tr>
            <td style="background:{color};padding:20px 28px;">
              <div style="color:#fff;font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:0.8;margin-bottom:6px;">Eco-Tracker Alert</div>
              <div style="color:#fff;font-size:22px;font-weight:700;">{icon} {alert_type}</div>
            </td>
          </tr>
          <tr>
            <td style="padding:26px 28px;">
              <p style="margin:0 0 6px;font-size:12px;color:#999;text-transform:uppercase;letter-spacing:1px;">Product</p>
              <p style="margin:0 0 20px;font-size:18px;font-weight:700;color:#222;">{product_name}</p>
              <div style="background:#f8f9f8;border-left:4px solid {color};padding:14px 16px;border-radius:0 8px 8px 0;">
                <p style="margin:0;font-size:14px;color:#333;line-height:1.6;">{message}</p>
              </div>
              <p style="margin:20px 0 0;font-size:11px;color:#bbb;">🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')} &nbsp;|&nbsp; Automated alert.</p>
            </td>
          </tr>
        </table>
      </td></tr>
    </table>
    </body></html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = Config.EMAIL_SENDER
    msg["To"] = to_address
    msg.attach(MIMEText(html, "html"))
    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            server.starttls()
            server.login(Config.EMAIL_SENDER, Config.EMAIL_PASSWORD)
            server.sendmail(Config.EMAIL_SENDER, to_address, msg.as_string())
        return True
    except Exception as e:
        print(f"Email Error: {e}")
        return False

def run_expiry_check():
    conn = raw_db()
    now = datetime.now()
    products = conn.execute("SELECT * FROM products").fetchall()
    for row in products:
        p = dict(row)
        store = p["store_name"]
        owners = conn.execute("SELECT email FROM users WHERE role = 'owner' AND store_name = ?", (store,)).fetchall()
        owner_emails = [o["email"] for o in owners]
        if not owner_emails: continue
        if p["exp"]:
            try:
                expiry_dt = datetime.strptime(p["exp"], "%Y-%m-%d")
                days_left = (expiry_dt - now).days
            except ValueError: continue
            if days_left < 0:
                _alert(conn, p, owner_emails, "Expired", "Product has expired.")
            elif days_left <= 2:
                _alert(conn, p, owner_emails, "Expiry 48h", f"{round(days_left * 24)}h remaining.")
            elif days_left <= 7:
                _alert(conn, p, owner_emails, "Expiry 7d", f"{days_left} days remaining.")
        qty, mn = p["qty"], p["min_qty"] or 0
        if qty == 0:
            _alert(conn, p, owner_emails, "Out of Stock", "Zero units remaining.")
        elif mn > 0 and qty <= mn:
            _alert(conn, p, owner_emails, "Low Stock", f"Only {qty} units left.")
    conn.commit()
    conn.close()

def _alert(conn, product, emails, alert_type, message):
    for email in emails:
        if send_email(email, product["name"], alert_type, message):
            conn.execute("INSERT INTO email_log (store_name, product_id, type, name, msg, sent_to) VALUES (?, ?, ?, ?, ?, ?)",
                         (product["store_name"], product["id"], alert_type, product["name"], message, email))

# ── FLASK ROUTES ──────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = Config.JWT_SECRET_KEY
jwt = JWTManager(app)
app.teardown_appcontext(close_db)

def owner_only(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if get_jwt_identity().get("role") != "owner":
            return jsonify({"error": "Only store owners can do this"}), 403
        return fn(*args, **kwargs)
    return wrapper

@app.route('/')
@app.route('/store')
def view_store():
    return render_template('store.html')

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    for field in ("name", "email", "store_name", "role", "password"):
        if not data.get(field): return jsonify({"error": f"'{field}' is required"}), 400
    if data["role"] not in ("owner", "manager"): return jsonify({"error": "Invalid role"}), 400
    if len(data["password"]) < 6: return jsonify({"error": "Password too short"}), 400
    hashed = bcrypt.hashpw(data["password"].encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    db = get_db()
    try:
        db.execute("INSERT INTO users (name, email, store_name, role, password) VALUES (?, ?, ?, ?, ?)",
                   (data["name"], data["email"].lower(), data["store_name"], data["role"], hashed))
        db.commit()
        return jsonify({"message": "Account created"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already registered"}), 409

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json() or {}
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (data.get("email", "").lower(),)).fetchone()
    if not user or not bcrypt.checkpw(data.get("password", "").encode("utf-8"), user["password"].encode("utf-8")):
        return jsonify({"error": "Incorrect email or password"}), 401
    token = create_access_token(identity={"id": user["id"], "name": user["name"], "email": user["email"], "store_name": user["store_name"], "role": user["role"]},
                                expires_delta=timedelta(hours=Config.SESSION_HOURS))
    return jsonify({"token": token, "user": {"id": user["id"], "name": user["name"], "role": user["role"]}}), 200

@app.route("/api/dashboard", methods=["GET"])
@jwt_required()
def dashboard():
    store = get_jwt_identity()["store_name"]
    products = [dict(r) for r in get_db().execute("SELECT * FROM products WHERE store_name = ?", (store,)).fetchall()]
    now = datetime.now()
    for p in products:
        try:
            p["days_left"] = (datetime.strptime(p["exp"], "%Y-%m-%d") - now).days if p["exp"] else None
        except:
            p["days_left"] = None
    metrics = {
        "total": len(products),
        "expired_or_critical": sum(1 for p in products if p["days_left"] is not None and p["days_left"] <= 2),
        "expiring_this_week": sum(1 for p in products if p["days_left"] is not None and 2 < p["days_left"] <= 7),
        "out_of_stock": sum(1 for p in products if p["qty"] == 0),
        "low_stock": sum(1 for p in products if 0 < p["qty"] <= (p["min_qty"] or 0)),
        "total_units": sum(p["qty"] for p in products)
    }
    return jsonify({"metrics": metrics, "expiry_alerts": [p for p in products if p["days_left"] is not None and p["days_left"] <= 7]}), 200

@app.route("/api/products/", methods=["GET", "POST"])
@jwt_required()
def handle_products():
    identity = get_jwt_identity()
    db = get_db()
    if request.method == "GET":
        res = [dict(r) for r in db.execute("SELECT * FROM products WHERE store_name = ?", (identity["store_name"],)).fetchall()]
        return jsonify(res), 200
    data = request.get_json() or {}
    cur = db.execute("INSERT INTO products (store_name, name, batch, cat, qty, min_qty, exp, price, loc, added_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (identity["store_name"], data["name"], data.get("batch"), data.get("cat", "Other"), int(data.get("qty", 0)), int(data.get("min", 0)), data.get("exp"), float(data.get("price", 0.0)), data.get("loc", ""), identity["id"]))
    db.commit()
    return jsonify({"id": cur.lastrowid}), 201

@app.route("/api/products/<int:product_id>", methods=["DELETE"])
@jwt_required()
def delete_product(product_id):
    get_db().execute("DELETE FROM products WHERE id = ? AND store_name = ?", (product_id, get_jwt_identity()["store_name"]))
    get_db().commit()
    return jsonify({"message": "Removed"}), 200

@app.route("/api/products/<int:product_id>/stock", methods=["PATCH"])
@jwt_required()
def update_stock(product_id):
    db = get_db()
    p = db.execute("SELECT qty FROM products WHERE id = ?", (product_id,)).fetchone()
    new_qty = p["qty"] + request.get_json().get("change", 0)
    if new_qty < 0: return jsonify({"error": "Negative stock"}), 400
    db.execute("UPDATE products SET qty = ?, updated_at = ? WHERE id = ?", (new_qty, datetime.now().isoformat(), product_id))
    db.commit()
    return jsonify({"new_qty": new_qty}), 200

@app.route("/api/email-log/", methods=["GET", "DELETE"])
@jwt_required()
def handle_logs():
    store = get_jwt_identity()["store_name"]
    db = get_db()
    if request.method == "DELETE":
        db.execute("DELETE FROM email_log WHERE store_name = ?", (store,))
        db.commit()
        return jsonify({"message": "Cleared"}), 200
    logs = [dict(r) for r in db.execute("SELECT * FROM email_log WHERE store_name = ? ORDER BY logged_at DESC LIMIT 60", (store,)).fetchall()]
    return jsonify(logs), 200

@app.route("/api/accounts/", methods=["GET"])
@jwt_required()
@owner_only
def list_accounts():
    res = [dict(r) for r in get_db().execute("SELECT id, name, email, role, joined_at FROM users WHERE store_name = ?", (get_jwt_identity()["store_name"],)).fetchall()]
    return jsonify(res), 200

@app.route("/api/accounts/<int:user_id>", methods=["DELETE"])
@jwt_required()
@owner_only
def remove_account(user_id):
    if get_jwt_identity()["id"] == user_id: return jsonify({"error": "Cannot delete self"}), 400
    get_db().execute("DELETE FROM users WHERE id = ?", (user_id,))
    get_db().commit()
    return jsonify({"message": "Removed"}), 200

@app.route("/api/alerts/trigger", methods=["POST"])
@jwt_required()
def trigger_check():
    run_expiry_check()
    return jsonify({"message": "Check completed"}), 200

# ── RUNNER ───────────────────────────────────────────────────────────────────
init_db()
scheduler = BackgroundScheduler()
scheduler.add_job(func=run_expiry_check, trigger="interval", hours=1, id="expiry_check", max_instances=1)
scheduler.start()

if __name__ == "__main__":
    # Use the port Render tells us to use, or 5000 if local
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

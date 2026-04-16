import sqlite3
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps

# ── Third-party (pip install ...) ─────────────────────────────────────────────
from flask import Flask, request, jsonify, g
from flask_jwt_extended import (
    JWTManager, create_access_token,
    jwt_required, get_jwt_identity
)
import bcrypt
from apscheduler.schedulers.background import BackgroundScheduler


# ==============================================================================
#  ⚙️  SECTION 1 — CONFIGURATION
#  Edit these settings before running for the first time.
# ==============================================================================

class Config:

    JWT_SECRET_KEY = "eco-tracker-secret-change-me-2024"

    SESSION_HOURS = 24

    DATABASE = "eco_tracker.db"

    EMAIL_SENDER   = "your_email@gmail.com"
    EMAIL_PASSWORD = "your_app_password_here"
    SMTP_HOST      = "smtp.gmail.com"
    SMTP_PORT      = 587

    LOW_STOCK_DEFAULT_MIN = 10


def get_db():
    """
    Return a database connection for the current request.
    Flask's 'g' object stores it so we reuse one connection per request.
    """
    if "db" not in g:
        g.db = sqlite3.connect(Config.DATABASE)
        # Allows accessing columns by name: row["name"] instead of row[0]
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(e=None):
    """Close the database connection at the end of every request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def raw_db():
    """
    Open a fresh database connection for use OUTSIDE of Flask requests.
    (Background jobs run outside requests, so they can't use get_db/g.)
    """
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """
    Create all database tables on first run.
    Safe to call on every startup — IF NOT EXISTS means it won't overwrite data.
    """
    conn = raw_db()
    c    = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          INTEGER  PRIMARY KEY AUTOINCREMENT,
            name        TEXT     NOT NULL,
            email       TEXT     NOT NULL UNIQUE,
            store_name  TEXT     NOT NULL,
            password    TEXT     NOT NULL,   -- bcrypt hash, never plain text
            role        TEXT     NOT NULL DEFAULT 'manager',  -- 'owner' or 'manager'
            joined_at   TEXT     DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER  PRIMARY KEY AUTOINCREMENT,
            store_name  TEXT     NOT NULL,       -- products are scoped to a store
            name        TEXT     NOT NULL,
            batch       TEXT,
            cat         TEXT     DEFAULT 'Other',
            qty         INTEGER  NOT NULL DEFAULT 0,
            min_qty     INTEGER  DEFAULT 0,       -- minimum stock level (triggers Low Stock alert)
            exp         TEXT,                     -- expiry date: 'YYYY-MM-DD'
            price       REAL     DEFAULT 0.0,
            loc         TEXT,
            added_by    INTEGER  REFERENCES users(id),
            created_at  TEXT     DEFAULT (datetime('now')),
            updated_at  TEXT     DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS email_log (
            id          INTEGER  PRIMARY KEY AUTOINCREMENT,
            store_name  TEXT     NOT NULL,
            product_id  INTEGER  REFERENCES products(id),
            type        TEXT     NOT NULL,   -- e.g. 'Expired', 'Expiry 48h', 'Low Stock'
            name        TEXT     NOT NULL,   -- product name at time of alert
            msg         TEXT,               -- short description shown in the frontend log
            sent_to     TEXT,               -- email address it was delivered to
            logged_at   TEXT     DEFAULT (datetime('now'))
        )
    """)

    conn.commit()
    conn.close()
    print("✅  Database ready.")

ALERT_STYLES = {
    "Expired":      ("#e74c3c", "🚨"),
    "Expiry 48h":   ("#e74c3c", "⏰"),
    "Expiry 7d":    ("#f39c12", "📅"),
    "Out of Stock": ("#e74c3c", "❌"),
    "Low Stock":    ("#9b59b6", "📦"),
}


def send_email(to_address, product_name, alert_type, message):
    """
    Send one HTML alert email.

    Parameters
    ----------
    to_address   : str  — recipient's email address
    product_name : str  — product that triggered the alert
    alert_type   : str  — one of the 5 types listed in ALERT_STYLES
    message      : str  — short description to show in the email body

    Returns True if sent successfully, False if it failed.
    """
    color, icon = ALERT_STYLES.get(alert_type, ("#27ae60", "ℹ️"))
    subject     = f"[Eco-Tracker] {alert_type} — {product_name}"

    html = f"""
    <html><body style="margin:0;padding:0;background:#f0f4f0;font-family:Arial,sans-serif;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr><td align="center" style="padding:32px 12px;">
        <table width="520" cellpadding="0" cellspacing="0"
               style="background:#fff;border-radius:12px;
                      box-shadow:0 4px 18px rgba(0,0,0,0.1);overflow:hidden;">

          <!-- Colour header bar -->
          <tr>
            <td style="background:{color};padding:20px 28px;">
              <div style="color:#fff;font-size:11px;letter-spacing:2px;
                           text-transform:uppercase;opacity:0.8;margin-bottom:6px;">
                Eco-Tracker Alert
              </div>
              <div style="color:#fff;font-size:22px;font-weight:700;">
                {icon} {alert_type}
              </div>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:26px 28px;">
              <p style="margin:0 0 6px;font-size:12px;color:#999;
                         text-transform:uppercase;letter-spacing:1px;">Product</p>
              <p style="margin:0 0 20px;font-size:18px;font-weight:700;color:#222;">
                {product_name}
              </p>
              <div style="background:#f8f9f8;border-left:4px solid {color};
                           padding:14px 16px;border-radius:0 8px 8px 0;">
                <p style="margin:0;font-size:14px;color:#333;line-height:1.6;">
                  {message}
                </p>
              </div>
              <p style="margin:20px 0 0;font-size:11px;color:#bbb;">
                🕐 {datetime.now().strftime('%d %b %Y, %I:%M %p')} &nbsp;|&nbsp;
                Automated alert from your Eco Store Inventory System.
              </p>
            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f8fbf8;padding:12px 28px;text-align:center;">
              <p style="margin:0;font-size:11px;color:#aaa;">🌿 Eco-Tracker · Inventory &amp; Expiry System</p>
            </td>
          </tr>

        </table>
      </td></tr>
    </table>
    </body></html>
    """

    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = Config.EMAIL_SENDER
    msg["To"]      = to_address
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT) as server:
            server.starttls()   # Encrypt the connection before sending credentials
            server.login(Config.EMAIL_SENDER, Config.EMAIL_PASSWORD)
            server.sendmail(Config.EMAIL_SENDER, to_address, msg.as_string())
        print(f"  ✉️   Email sent → {to_address} [{alert_type}] {product_name}")
        return True
    except Exception as err:
        print(f"  ❌  Email failed: {err}")
        return False

def run_expiry_check():
    """
    Hourly background job:
      For every product in the database, check expiry date and stock level.
      If an alert condition is found, email all owners in that store and log it.

    Alert conditions checked (same as frontend scanEmails() function):
        Expired      — expiry date is in the past
        Expiry 48h   — expires within 2 days
        Expiry 7d    — expires within 7 days
        Out of Stock — quantity is 0
        Low Stock    — quantity > 0 but <= min_qty
    """
    print(f"\n🔍  Running expiry check — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    conn = raw_db()
    now  = datetime.now()

    products = conn.execute("SELECT * FROM products").fetchall()

    alerts_sent = 0

    for row in products:
        p   = dict(row)
        store = p["store_name"]

        # Get all owner email addresses for this store
        owners = conn.execute(
            "SELECT email FROM users WHERE role = 'owner' AND store_name = ?",
            (store,)
        ).fetchall()
        owner_emails = [o["email"] for o in owners]

        if not owner_emails:
            continue   # No owners registered for this store yet

        if p["exp"]:
            try:
                expiry_dt  = datetime.strptime(p["exp"], "%Y-%m-%d")
                # Count days remaining (same formula as frontend: days(exp))
                days_left  = (expiry_dt - now).days
            except ValueError:
                print(f"  ⚠️   Bad expiry date for product id={p['id']}, skipping.")
                continue

            if days_left < 0:
                msg = "Product has expired — please remove from shelves immediately."
                _alert(conn, p, owner_emails, "Expired", msg)
                alerts_sent += 1

            elif days_left <= 2:
                hours_left = round(days_left * 24)
                msg = f"{hours_left}h remaining. Urgent action needed."
                _alert(conn, p, owner_emails, "Expiry 48h", msg)
                alerts_sent += 1

            elif days_left <= 7:
                msg = f"{days_left} days remaining. Plan disposal or discount."
                _alert(conn, p, owner_emails, "Expiry 7d", msg)
                alerts_sent += 1

        qty = p["qty"]
        mn  = p["min_qty"] or 0

        if qty == 0:
            _alert(conn, p, owner_emails, "Out of Stock",
                   "Zero units remaining — restock immediately.")
            alerts_sent += 1

        elif mn > 0 and qty <= mn:
            _alert(conn, p, owner_emails, "Low Stock",
                   f"Only {qty} units left (minimum: {mn}).")
            alerts_sent += 1

    conn.commit()
    conn.close()
    print(f"✅  Check complete. Alerts sent: {alerts_sent}\n")


def _alert(conn, product, emails, alert_type, message):
    """
    Helper: email every owner in the list, then save a record in email_log.
    """
    for email in emails:
        ok = send_email(email, product["name"], alert_type, message)
        if ok:
            conn.execute("""
                INSERT INTO email_log
                  (store_name, product_id, type, name, msg, sent_to)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (product["store_name"], product["id"],
                  alert_type, product["name"], message, email))


app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = Config.JWT_SECRET_KEY
jwt = JWTManager(app)

# Close database after every request automatically
app.teardown_appcontext(close_db)


def owner_only(fn):
    """
    Decorator that blocks Managers from accessing owner-only endpoints.
    Place it BELOW @jwt_required() on any route.

    Example:
        @app.route("/api/accounts/")
        @jwt_required()
        @owner_only
        def list_accounts():
            ...
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        me = get_jwt_identity()
        if me.get("role") != "owner":
            return jsonify({"error": "Only store owners can do this"}), 403
        return fn(*args, **kwargs)
    return wrapper


def me():
    """Shortcut: returns the logged-in user's identity dict from the JWT token."""
    return get_jwt_identity()

@app.route("/api/auth/register", methods=["POST"])
def register():
    """
    Create a new user account.

    Request JSON:
        name       : string   Full name (e.g. "Ravi Kumar")
        email      : string   Login email
        store_name : string   Store name (e.g. "Green Mart - Bhimavaram")
        role       : string   "owner" or "manager"
        password   : string   Min 6 characters

    Response:
        201  { "message": "Account created" }
        400  { "error": "..." }    missing field / invalid role / password too short
        409  { "error": "..." }    email already registered
    """
    data = request.get_json() or {}

    for field in ("name", "email", "store_name", "role", "password"):
        if not data.get(field):
            return jsonify({"error": f"'{field}' is required"}), 400

    if data["role"] not in ("owner", "manager"):
        return jsonify({"error": "role must be 'owner' or 'manager'"}), 400

    if len(data["password"]) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400

    hashed = bcrypt.hashpw(
        data["password"].encode("utf-8"),
        bcrypt.gensalt()
    ).decode("utf-8")

    db = get_db()
    try:
        db.execute(
            """INSERT INTO users (name, email, store_name, role, password)
               VALUES (?, ?, ?, ?, ?)""",
            (data["name"], data["email"].lower(),
             data["store_name"], data["role"], hashed)
        )
        db.commit()
        return jsonify({"message": "Account created successfully"}), 201

    except sqlite3.IntegrityError:
        # The UNIQUE constraint on email was violated
        return jsonify({"error": "That email address is already registered"}), 409


@app.route("/api/auth/login", methods=["POST"])
def login():
    """
    Log in with email + password. Returns a JWT token on success.

    Request JSON:
        email    : string
        password : string

    Response:
        200  { "token": "...", "user": { id, name, email, store_name, role } }
        400  { "error": "..." }   missing fields
        401  { "error": "..." }   wrong email or password
    """
    data = request.get_json() or {}

    if not data.get("email") or not data.get("password"):
        return jsonify({"error": "Email and password are required"}), 400

    db   = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE email = ?",
        (data["email"].lower(),)
    ).fetchone()

    # Check: user exists AND password matches the stored hash
    if not user or not bcrypt.checkpw(
        data["password"].encode("utf-8"),
        user["password"].encode("utf-8")
    ):
        return jsonify({"error": "Incorrect email or password"}), 401

    token = create_access_token(
        identity={
            "id":         user["id"],
            "name":       user["name"],
            "email":      user["email"],
            "store_name": user["store_name"],
            "role":       user["role"],
        },
        expires_delta=timedelta(hours=Config.SESSION_HOURS)
    )

    return jsonify({
        "token": token,
        "user": {
            "id":         user["id"],
            "name":       user["name"],
            "email":      user["email"],
            "store_name": user["store_name"],
            "role":       user["role"],
        }
    }), 200


@app.route("/api/dashboard", methods=["GET"])
@jwt_required()
def dashboard():
    """
    Returns all data needed to render the Dashboard tab.

    Response:
        {
          "metrics": {
            total, expired_or_critical, expiring_this_week,
            out_of_stock, low_stock, total_units
          },
          "expiry_alerts": [ { id, name, batch, cat, loc, exp, days_left }, ... ],
          "stock_alerts":  [ { id, name, qty, min_qty, out_of_stock }, ... ]
        }
    """
    store = me()["store_name"]
    db    = get_db()
    now   = datetime.now()

    products = db.execute(
        "SELECT * FROM products WHERE store_name = ?", (store,)
    ).fetchall()

    enriched = []
    for row in products:
        p = dict(row)
        p["days_left"] = None
        if p["exp"]:
            try:
                expiry_dt    = datetime.strptime(p["exp"], "%Y-%m-%d")
                p["days_left"] = (expiry_dt - now).days
            except ValueError:
                pass
        enriched.append(p)

    total           = len(enriched)
    expired_crit    = sum(1 for p in enriched
                         if p["days_left"] is not None and p["days_left"] <= 2)
    expiring_week   = sum(1 for p in enriched
                         if p["days_left"] is not None
                         and 2 < p["days_left"] <= 7)
    out_of_stock    = sum(1 for p in enriched if p["qty"] == 0)
    low_stock_count = sum(1 for p in enriched
                         if p["qty"] > 0
                         and p["min_qty"] > 0
                         and p["qty"] <= p["min_qty"])
    total_units     = sum(p["qty"] for p in enriched)

    expiry_alerts = sorted(
        [p for p in enriched if p["days_left"] is not None and p["days_left"] <= 7],
        key=lambda p: p["days_left"]
    )

    stock_alerts = sorted(
        [p for p in enriched
         if p["qty"] == 0 or (p["min_qty"] > 0 and p["qty"] <= p["min_qty"])],
        key=lambda p: p["qty"]
    )

    return jsonify({
        "metrics": {
            "total":               total,
            "expired_or_critical": expired_crit,
            "expiring_this_week":  expiring_week,
            "out_of_stock":        out_of_stock,
            "low_stock":           low_stock_count,
            "total_units":         total_units,
        },
        "expiry_alerts": expiry_alerts,
        "stock_alerts":  stock_alerts,
    }), 200

@app.route("/api/products/", methods=["GET"])
@jwt_required()
def list_products():
    """
    Return all products for this store.

    Optional query parameters (match frontend Inventory tab filters):
        q   : string   — search term (matches name or batch)
        f   : string   — 'exp' (expired/critical only) | 'low' (low/out of stock)

    Response:
        [ { id, name, batch, cat, qty, min_qty, exp, price, loc, days_left }, ... ]
    """
    store = me()["store_name"]
    db    = get_db()
    now   = datetime.now()

    search = (request.args.get("q") or "").lower()
    filt   = request.args.get("f") or ""

    products = db.execute(
        "SELECT * FROM products WHERE store_name = ?", (store,)
    ).fetchall()

    result = []
    for row in products:
        p = dict(row)

        p["days_left"] = None
        if p["exp"]:
            try:
                p["days_left"] = (datetime.strptime(p["exp"], "%Y-%m-%d") - now).days
            except ValueError:
                pass

        if search:
            if search not in p["name"].lower() and search not in (p["batch"] or "").lower():
                continue

        if filt == "exp":
            # Show only expired or expiring within 48h (Critical)
            if p["days_left"] is None or p["days_left"] > 2:
                continue

        elif filt == "low":
            # Show only out-of-stock or low-stock items
            is_low = p["qty"] > 0 and p["min_qty"] > 0 and p["qty"] <= p["min_qty"]
            if p["qty"] != 0 and not is_low:
                continue

        result.append(p)

    result.sort(key=lambda p: (p["exp"] is None, p["exp"]))

    return jsonify(result), 200


@app.route("/api/products/", methods=["POST"])
@jwt_required()
def add_product():
    """
    Add a new product to inventory.
    Works for both manual entry (Scan tab form) and QR scan data.

    Request JSON — mirrors the frontend saveItem() fields:
        name  : string   (required) — product name
        batch : string              — batch ID (auto-generated if empty)
        cat   : string              — category (defaults to 'Other')
        qty   : integer             — quantity (defaults to 0)
        min   : integer             — minimum stock alert level
        exp   : string              — expiry date 'YYYY-MM-DD'
        price : float               — unit price in ₹
        loc   : string              — shelf/location

    Response:
        201  { "message": "Product added", "id": <new_id> }
        400  { "error": "..." }
    """
    identity = me()
    data     = request.get_json() or {}

    if not data.get("name"):
        return jsonify({"error": "'name' is required"}), 400

    # Validate expiry date format if provided
    if data.get("exp"):
        try:
            datetime.strptime(data["exp"], "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": "exp (expiry date) must be in YYYY-MM-DD format"}), 400

    db = get_db()
    try:
        cur = db.execute("""
            INSERT INTO products
              (store_name, name, batch, cat, qty, min_qty, exp, price, loc, added_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            identity["store_name"],
            data["name"],
            data.get("batch") or ("B-" + str(int(datetime.now().timestamp()))[-4:]),
            data.get("cat", "Other"),
            int(data.get("qty", 0)),
            int(data.get("min", 0)),
            data.get("exp"),
            float(data.get("price", 0.0)),
            data.get("loc", ""),
            identity["id"]
        ))
        db.commit()
        return jsonify({"message": "Product added", "id": cur.lastrowid}), 201

    except Exception as err:
        return jsonify({"error": str(err)}), 400


@app.route("/api/products/<int:product_id>", methods=["DELETE"])
@jwt_required()
def delete_product(product_id):
    """
    Remove a product from inventory.
    (Frontend: the ✕ button in the Inventory table calls this)

    Both owners and managers can delete products.
    """
    store = me()["store_name"]
    db    = get_db()

    product = db.execute(
        "SELECT id FROM products WHERE id = ? AND store_name = ?",
        (product_id, store)
    ).fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    return jsonify({"message": "Product removed"}), 200


@app.route("/api/products/<int:product_id>/stock", methods=["PATCH"])
@jwt_required()
def update_stock(product_id):
    """
    Adjust a product's stock quantity.

    Request JSON:
        change : integer   Positive = add stock, Negative = remove stock.
                           e.g. { "change": 20 } adds 20 units
                                { "change": -5 } removes 5 units

    Response:
        200  { "message": "Stock updated", "new_qty": <number> }
        400  { "error": "..." }   would go below 0
        404  { "error": "..." }   product not found
    """
    store = me()["store_name"]
    data  = request.get_json() or {}

    change = data.get("change", 0)
    if not isinstance(change, int):
        return jsonify({"error": "'change' must be a whole number"}), 400

    db      = get_db()
    product = db.execute(
        "SELECT * FROM products WHERE id = ? AND store_name = ?",
        (product_id, store)
    ).fetchone()

    if not product:
        return jsonify({"error": "Product not found"}), 404

    new_qty = product["qty"] + change
    if new_qty < 0:
        return jsonify({
            "error": f"Cannot remove {abs(change)} — only {product['qty']} in stock"
        }), 400

    db.execute(
        "UPDATE products SET qty = ?, updated_at = ? WHERE id = ?",
        (new_qty, datetime.now().isoformat(), product_id)
    )
    db.commit()
    return jsonify({"message": "Stock updated", "new_qty": new_qty}), 200

@app.route("/api/email-log/", methods=["GET"])
@jwt_required()
def get_email_log():
    """
    Return all email alert records for this store, newest first.
    (Matches the frontend's Email Log tab exactly)

    Response:
        [
          {
            "id": 1,
            "type": "Expired",
            "name": "Organic Milk",
            "msg":  "Product has expired ...",
            "sent_to": "owner@store.com",
            "logged_at": "2024-01-15 09:30:00"
          },
          ...
        ]
    """
    store = me()["store_name"]
    db    = get_db()

    logs = db.execute("""
        SELECT id, type, name, msg, sent_to, logged_at
        FROM   email_log
        WHERE  store_name = ?
        ORDER  BY logged_at DESC
        LIMIT  60
    """, (store,)).fetchall()

    # Add a "time" field formatted like the frontend shows (HH:MM:SS AM/PM)
    result = []
    for row in logs:
        entry = dict(row)
        try:
            dt = datetime.fromisoformat(entry["logged_at"])
            entry["time"] = dt.strftime("%I:%M:%S %p")
            entry["date"] = dt.strftime("%d/%m/%Y")
        except Exception:
            entry["time"] = entry["logged_at"]
            entry["date"] = ""
        result.append(entry)

    return jsonify(result), 200


@app.route("/api/email-log/", methods=["DELETE"])
@jwt_required()
def clear_email_log():
    """
    Clear all email alert records for this store.
    (Frontend: "Clear" button on the Email Log tab)
    """
    store = me()["store_name"]
    db    = get_db()
    db.execute("DELETE FROM email_log WHERE store_name = ?", (store,))
    db.commit()
    return jsonify({"message": "Email log cleared"}), 200

@app.route("/api/accounts/", methods=["GET"])
@jwt_required()
@owner_only
def list_accounts():
    """
    Return all user accounts belonging to this store.
    (Owner only — hidden from managers in the frontend nav)

    Response:
        [
          { "id": 1, "name": "Ravi", "email": "...", "role": "owner", "joined": "15 Jan 2024" },
          ...
        ]
    """
    store = me()["store_name"]
    db    = get_db()

    users = db.execute("""
        SELECT id, name, email, role, joined_at
        FROM   users
        WHERE  store_name = ?
        ORDER  BY joined_at DESC
    """, (store,)).fetchall()

    result = []
    for row in users:
        u = dict(row)
        # Format the joined date like the frontend shows: "15 Jan 2024"
        try:
            dt = datetime.fromisoformat(u["joined_at"])
            u["joined"] = dt.strftime("%d %b %Y")
        except Exception:
            u["joined"] = u["joined_at"]
        result.append(u)

    return jsonify(result), 200


@app.route("/api/accounts/<int:user_id>", methods=["DELETE"])
@jwt_required()
@owner_only
def remove_account(user_id):
    """
    Remove a user account from this store.
    (Owner only — the "Remove" button in the Accounts table)

    Owners cannot delete their own account.
    """
    myself = me()

    if myself["id"] == user_id:
        return jsonify({"error": "You cannot remove your own account"}), 400

    store = myself["store_name"]
    db    = get_db()

    target = db.execute(
        "SELECT id FROM users WHERE id = ? AND store_name = ?",
        (user_id, store)
    ).fetchone()

    if not target:
        return jsonify({"error": "Account not found in your store"}), 404

    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"message": "Account removed"}), 200

@app.route("/api/alerts/trigger", methods=["POST"])
@jwt_required()
def trigger_check():
    """
    Run the expiry/stock check immediately.
    Both owners and managers can trigger this.
    """
    run_expiry_check()
    return jsonify({"message": "Expiry check completed"}), 200

if __name__ == "__main__":

    # 1️⃣  Create database tables
    init_db()

    # 2️⃣  Start the hourly background scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(
        func          = run_expiry_check,
        trigger       = "interval",
        hours         = 1,
        id            = "expiry_check",
        max_instances = 1   # Never run two checks at the same time
    )
    scheduler.start()
    print("⏰  Hourly expiry monitor started.")

    # 3️⃣  Run once at startup so you see results immediately
    run_expiry_check()

    # 4️⃣  Print the API reference and start the server
    print("""
╔══════════════════════════════════════════════════════════════╗
║  🌿  Eco-Tracker Backend  →  http://localhost:5000          ║
╠══════════════════════════════════════════════════════════════╣
║  AUTH                                                        ║
║    POST   /api/auth/register                                 ║
║    POST   /api/auth/login                                    ║
║                                                              ║
║  DASHBOARD         (JWT required)                            ║
║    GET    /api/dashboard                                     ║
║                                                              ║
║  PRODUCTS          (JWT required)                            ║
║    GET    /api/products/              (search: ?q=&f=)       ║
║    POST   /api/products/                                     ║
║    DELETE /api/products/<id>                                 ║
║    PATCH  /api/products/<id>/stock                           ║
║                                                              ║
║  EMAIL LOG         (JWT required)                            ║
║    GET    /api/email-log/                                    ║
║    DELETE /api/email-log/                                    ║
║                                                              ║
║  ACCOUNTS          (JWT + Owner only)                        ║
║    GET    /api/accounts/                                     ║
║    DELETE /api/accounts/<id>                                 ║
║                                                              ║
║  ALERTS            (JWT required)                            ║
║    POST   /api/alerts/trigger                                ║
╚══════════════════════════════════════════════════════════════╝
    """)

    app.run(debug=True, port=5000)

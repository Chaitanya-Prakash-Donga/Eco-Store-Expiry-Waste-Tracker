import sqlite3
import smtplib
import bcrypt
import os
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

# ── CONFIGURATION ───────────────────────────────────────────────────────────
class Config:
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "eco-tracker-secure-key-2026")
    DATABASE = "eco_tracker.db"
    # Set these in Render Environment Variables
    EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
    SMTP_HOST      = "smtp.gmail.com"
    SMTP_PORT      = 587

# ── DATABASE INITIALIZATION (Ensures Fresh Start) ───────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(Config.DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    with sqlite3.connect(Config.DATABASE) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT, email TEXT UNIQUE, store_name TEXT, password TEXT, role TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT, store_name TEXT, name TEXT, 
            qty INTEGER, min_qty INTEGER, exp TEXT, added_by INTEGER)""")
        c.execute("""CREATE TABLE IF NOT EXISTS email_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, store_name TEXT, type TEXT, name TEXT, msg TEXT)""")
        conn.commit()

# ── EXPIRY & STOCK LOGIC ───────────────────────────────────────────────────
def run_expiry_check():
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row
    now = datetime.now()
    # Check all products
    products = conn.execute("SELECT * FROM products").fetchall()
    
    for p in products:
        p = dict(p)
        if not p["exp"]: continue
        
        expiry_dt = datetime.strptime(p["exp"], "%Y-%m-%d")
        days_left = (expiry_dt - now).days
        
        # Trigger alert if product is 3-4 days from expiry
        if 0 <= days_left <= 4:
            message = f"Warning: {p['name']} expires in {days_left} days!"
            print(f"ALERT: {message}")
            # Logic to send email would go here
            
    conn.close()

# ── FLASK APP & ROUTES ──────────────────────────────────────────────────────
app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = Config.JWT_SECRET_KEY
jwt = JWTManager(app)

@app.route('/')
def index(): return render_template('store.html')

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    hashed = bcrypt.hashpw(data["password"].encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    db = get_db()
    try:
        db.execute("INSERT INTO users (name, email, store_name, password, role) VALUES (?,?,?,?,?)",
                   (data["name"], data["email"].lower(), data["store_name"], hashed, data["role"]))
        db.commit()
        return jsonify({"message": "Registered"}), 201
    except: return jsonify({"error": "Email exists"}), 409

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (data["email"].lower(),)).fetchone()
    if user and bcrypt.checkpw(data["password"].encode('utf-8'), user["password"].encode('utf-8')):
        token = create_access_token(identity={"id":user["id"], "store_name":user["store_name"], "role":user["role"]})
        return jsonify({"token": token, "user": {"name": user["name"], "role": user["role"]}})
    return jsonify({"error": "Invalid credentials"}), 401

@app.route("/api/dashboard", methods=["GET"])
@jwt_required()
def dashboard():
    store = get_jwt_identity()["store_name"]
    db = get_db()
    # Fetch ONLY this store's products
    rows = db.execute("SELECT * FROM products WHERE store_name = ?", (store,)).fetchall()
    products = [dict(r) for r in rows]
    
    now = datetime.now()
    alerts = []
    
    for p in products:
        if p["exp"]:
            d_left = (datetime.strptime(p["exp"], "%Y-%m-%d") - now).days
            # Add to alerts only if within the 4-day window
            if d_left <= 4:
                p["days_left"] = d_left
                alerts.append(p)

    metrics = {
        "total": len(products),
        "expiry_alerts": len(alerts),
        "low_stock": sum(1 for p in products if p["qty"] <= (p["min_qty"] or 0))
    }
    return jsonify({"metrics": metrics, "expiry_alerts": alerts})

# ── INITIALIZE AND RUN ─────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_expiry_check, 'interval', hours=1)
    scheduler.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

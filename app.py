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

# ── 1. CONFIGURATION ──────────────────────────────────────────────────────────
class Config:
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "eco-tracker-secure-key-2026")
    DATABASE = "eco_tracker.db"
    # Set these in Render Environment Variables for email features
    EMAIL_SENDER   = os.environ.get("EMAIL_SENDER")
    EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD")
    SMTP_HOST      = "smtp.gmail.com"
    SMTP_PORT      = 587

# ── 2. APP INITIALIZATION (Must be before decorators) ────────────────────────
app = Flask(__name__)
app.config["JWT_SECRET_KEY"] = Config.JWT_SECRET_KEY
jwt = JWTManager(app)

# ── 3. DATABASE HELPERS ──────────────────────────────────────────────────────
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

# ── 4. EXPIRY & STOCK LOGIC (Internal Background Tasks) ──────────────────────
def run_expiry_check():
    # This runs in the background to log alerts
    with sqlite3.connect(Config.DATABASE) as conn:
        conn.row_factory = sqlite3.Row
        now = datetime.now()
        products = conn.execute("SELECT * FROM products").fetchall()
        
        for p in products:
            if not p["exp"]: continue
            try:
                expiry_dt = datetime.strptime(p["exp"], "%Y-%m-%d")
                days_left = (expiry_dt - now).days
                # Logic to trigger background emails for 3-4 days could go here
            except: continue

# ── 5. API ROUTES ────────────────────────────────────────────────────────────

@app.route('/')
def index(): 
    return render_template('store.html')

@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json()
    # Password hashing
    hashed = bcrypt.hashpw(data["password"].encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
    db = get_db()
    try:
        db.execute("INSERT INTO users (name, email, store_name, password, role) VALUES (?,?,?,?,?)",
                   (data["name"], data["email"].lower(), data["store_name"], hashed, data["role"]))
        db.commit()
        return jsonify({"message": "Account created successfully"}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email already exists"}), 409

@app.route("/api/auth/login", methods=["POST"])
def login():
    data = request.get_json()
    user = get_db().execute("SELECT * FROM users WHERE email = ?", (data["email"].lower(),)).fetchone()
    
    if user and bcrypt.checkpw(data["password"].encode('utf-8'), user["password"].encode('utf-8')):
        token = create_access_token(identity={
            "id": user["id"], 
            "store_name": user["store_name"], 
            "role": user["role"]
        })
        return jsonify({"token": token, "user": {"name": user["name"], "role": user["role"]}})
    return jsonify({"error": "Invalid email or password"}), 401

@app.route("/api/dashboard", methods=["GET"])
@jwt_required()
def dashboard():
    store = get_jwt_identity()["store_name"]
    db = get_db()
    
    # 1. Fetch ONLY products for this specific store (Fresh account = Empty list)
    rows = db.execute("SELECT * FROM products WHERE store_name = ?", (store,)).fetchall()
    products = [dict(r) for r in rows]
    
    now = datetime.now()
    expiry_alerts = []
    low_stock_alerts = []
    
    for p in products:
        # 2. Expiry Logic (Specifically showing 3-4 days window)
        if p["exp"]:
            try:
                expiry_date = datetime.strptime(p["exp"], "%Y-%m-%d")
                d_left = (expiry_date - now).days
                
                # If product is within 4 days of expiring, add to alerts
                if d_left <= 4:
                    p["days_left"] = d_left
                    expiry_alerts.append(p)
            except: pass

        # 3. Stock Logic (If stock is replaced/increased, this alert disappears)
        if p["qty"] <= (p["min_qty"] or 0):
            low_stock_alerts.append(p)

    metrics = {
        "total_products": len(products),
        "expiry_count": len(expiry_alerts),
        "low_stock_count": len(low_stock_alerts),
        "total_stock": sum(p["qty"] for p in products)
    }
    
    return jsonify({
        "metrics": metrics, 
        "expiry_alerts": expiry_alerts,
        "low_stock_alerts": low_stock_alerts
    })

@app.route("/api/products", methods=["POST"])
@jwt_required()
def add_product():
    identity = get_jwt_identity()
    data = request.get_json()
    db = get_db()
    db.execute("""INSERT INTO products (store_name, name, qty, min_qty, exp, added_by) 
                  VALUES (?, ?, ?, ?, ?, ?)""",
               (identity["store_name"], data["name"], data["qty"], data.get("min", 0), data.get("exp"), identity["id"]))
    db.commit()
    return jsonify({"message": "Product added"}), 201

# ── 6. RUNNER ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    
    # Start background scheduler for automated checks
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_expiry_check, 'interval', hours=1)
    scheduler.start()
    
    # Render uses the PORT environment variable
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

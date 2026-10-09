from flask import Flask, redirect, render_template, request, jsonify, session
from functools import wraps
import os
import base64
from datetime import timedelta, datetime
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
from flask_socketio import SocketIO, join_room

# Reads a .env file sitting next to app.py and loads its KEY=value lines as
# environment variables. This is what actually fixes "not configured" —
# variables set with $env: in PowerShell (or set in cmd) only last for that
# one terminal session; a .env file is read fresh every time the app starts,
# so it survives closing VS Code, restarting, rebooting, etc.
load_dotenv()

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

app.secret_key = "mic-cheque-1-2"

app.config["SESSION_PERMANENT"] = True
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)
app.config["TEMPLATES_AUTO_RELOAD"] = False


# =====================================
# DATABASE (PostgreSQL)
# =====================================
# One DATABASE_URL covers local dev and production alike. Point it at a
# local Postgres while developing, and at a managed Postgres (Render,
# Supabase, Neon, RDS, etc.) once this is actually deployed, so several
# phones/tablets/PCs can hit the same server at once. SQLite locks the
# whole file per write, which is fine for one person testing locally but
# starts throwing "database is locked" once multiple devices place and pay
# for orders concurrently — that's the actual problem Postgres solves here.
#
# Local setup, one time:
#   createdb laundry_db
#   export DATABASE_URL=postgresql://laundry_user:laundry_pass@localhost:5432/laundry_db
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres.obvvtyuavziykoeysnme:[Obadiah.1234$]@aws-0-us-east-1.pooler.supabase.com:6543/postgres",
)


def get_db():
    """Every request opens its own connection — the simplest correct thing
    for a small app. If this gets busy, swap this for a connection pool
    (psycopg2.pool.SimpleConnectionPool, or move to SQLAlchemy) later."""
    return psycopg2.connect(DATABASE_URL)


def dict_cursor(conn):
    """Rows come back as dict-like objects, so templates keep reading
    o.id / o.status / o.kg by name — same as sqlite3.Row did before."""
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# =====================================
# PRICING
# =====================================
PRICE_PER_KG = float(os.environ.get("PRICE_PER_KG", 100))


# =====================================
# M-PESA (DARAJA) CONFIG
# =====================================
MPESA_BASE_URL = os.environ.get("MPESA_BASE_URL", "https://sandbox.safaricom.co.ke")
MPESA_CONSUMER_KEY = os.environ.get("MPESA_CONSUMER_KEY", "")
MPESA_CONSUMER_SECRET = os.environ.get("MPESA_CONSUMER_SECRET", "")
MPESA_SHORTCODE = os.environ.get("MPESA_SHORTCODE", "")
MPESA_PASSKEY = os.environ.get("MPESA_PASSKEY", "")
MPESA_CALLBACK_URL = os.environ.get("MPESA_CALLBACK_URL", "")


def normalize_phone(raw_phone):
    """M-Pesa wants 2547XXXXXXXX / 2541XXXXXXXX — no +, no leading 0."""
    digits = "".join(ch for ch in str(raw_phone) if ch.isdigit())
    if digits.startswith("0"):
        digits = "254" + digits[1:]
    elif digits.startswith("7") or digits.startswith("1"):
        digits = "254" + digits
    return digits


def mpesa_access_token():
    resp = requests.get(
        f"{MPESA_BASE_URL}/oauth/v1/generate?grant_type=client_credentials",
        auth=(MPESA_CONSUMER_KEY, MPESA_CONSUMER_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def mpesa_password_and_timestamp():
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    raw = f"{MPESA_SHORTCODE}{MPESA_PASSKEY}{timestamp}"
    password = base64.b64encode(raw.encode()).decode()
    return password, timestamp


# =====================================
# SMS (AFRICA'S TALKING)
# =====================================
# Used to notify a customer their order is ready, over SMS — reaches them
# whether or not they have the site open, or even have data/wifi at all,
# unlike the socket.io live updates elsewhere in this app which only work
# while they're actually looking at the page.
#
# Sign up at https://africastalking.com/, create an app, and you'll get a
# username + API key. For testing without spending real SMS credit, use
# the sandbox: username "sandbox", base URL
# https://api.sandbox.africastalking.com/version1/messaging, and add your
# own phone as a simulator number in the sandbox dashboard.
AT_USERNAME = os.environ.get("AT_USERNAME", "")
AT_API_KEY = os.environ.get("AT_API_KEY", "")
AT_SENDER_ID = os.environ.get("AT_SENDER_ID", "")  # optional, needs approval
AT_BASE_URL = os.environ.get(
    "AT_BASE_URL", "https://api.africastalking.com/version1/messaging"
)
# The link put in the SMS telling the customer where to go pay.
SITE_URL = os.environ.get("SITE_URL", "http://localhost:5001")


def send_sms(phone, message):
    """Sends one SMS via Africa's Talking. Deliberately never raises — a
    failed SMS should never block marking an order complete, it should
    just get logged so you can notice and retry manually if needed.
    Returns True/False so the caller can report it back to the admin."""
    if not AT_USERNAME or not AT_API_KEY:
        print("SMS not sent: AT_USERNAME/AT_API_KEY not set in .env")
        return False

    try:
        data = {
            "username": AT_USERNAME,
            "to": "+" + normalize_phone(phone),  # AT wants E.164 with the +
            "message": message,
        }
        if AT_SENDER_ID:
            data["from"] = AT_SENDER_ID

        resp = requests.post(
            AT_BASE_URL,
            data=data,
            headers={
                "apiKey": AT_API_KEY,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        print(f"SMS to {phone}: {result}")
        return True
    except Exception as e:
        print(f"Could not send SMS to {phone}: {e}")
        return False


# =====================================
# DECORATOR (defined first, before use)
# =====================================

def login_required(role=None, api=False):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user_id" not in session:
                if api:
                    return jsonify({"success": False, "message": "Not logged in"}), 401
                return redirect("/login")
            if role and session.get("role") != role:
                if api:
                    return jsonify({"success": False, "message": "Not authorized"}), 403
                return redirect("/login")
            return f(*args, **kwargs)
        return wrapper
    return decorator


# =====================================
# DATABASE INITIALIZATION
# =====================================

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY,
        fullname TEXT NOT NULL,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'CUSTOMER'
    )
    """)

    # Every customer's phone lives on their account now, captured once at
    # registration, so /add_order can fill it in automatically instead of
    # asking for name/phone on every single order.
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        customer TEXT,
        phone TEXT,
        service TEXT,
        status TEXT DEFAULT 'Pending'
    )
    """)

    # Postgres supports "ADD COLUMN IF NOT EXISTS" directly (9.6+), so this
    # doesn't need the try/except dance sqlite required.
    for ddl in (
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_status TEXT DEFAULT 'unpaid'",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_method TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS checkout_request_id TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS amount INTEGER",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS mpesa_receipt TEXT",
        "ALTER TABLE orders ADD COLUMN IF NOT EXISTS kg REAL",
    ):
        cur.execute(ddl)

    # Payment history — one row PER ORDER, kept up to date in place (see
    # log_payment's ON CONFLICT below) rather than a new row every time
    # something happens to that order's payment.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS payments(
        id SERIAL PRIMARY KEY,
        order_id INTEGER REFERENCES orders(id),
        method TEXT,       -- 'mpesa' or 'cash'
        amount INTEGER,
        status TEXT,       -- 'pending', 'paid', 'failed', 'cod'
        reference TEXT,    -- M-Pesa receipt number, or NULL for cash
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # --- Fix "violates foreign key constraint" on delete ---
    # The REFERENCES above create constraints with NO delete behavior
    # specified, which defaults to blocking the delete entirely. Recreate
    # both with ON DELETE CASCADE: deleting an order removes its payment
    # row with it; deleting a user removes their orders (and, by the same
    # cascade, those orders' payment rows) with them.
    try:
        cur.execute("ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_user_id_fkey")
        cur.execute(
            "ALTER TABLE orders ADD CONSTRAINT orders_user_id_fkey "
            "FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE"
        )
        cur.execute("ALTER TABLE payments DROP CONSTRAINT IF EXISTS payments_order_id_fkey")
        cur.execute(
            "ALTER TABLE payments ADD CONSTRAINT payments_order_id_fkey "
            "FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE"
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Could not update foreign key constraints: {e}")

    # --- Collapse any existing multi-row history down to one row per order ---
    # Older runs logged a new row per event (pending, paid, cod, ...), so an
    # order could have several rows by now. Keep only the most recent one
    # before the UNIQUE constraint below is added, or that constraint will
    # fail on the leftover duplicates.
    try:
        cur.execute("""
            DELETE FROM payments p1
            USING payments p2
            WHERE p1.order_id = p2.order_id
              AND p1.id < p2.id
        """)
        cur.execute(
            "ALTER TABLE payments ADD CONSTRAINT payments_order_id_key UNIQUE (order_id)"
        )
        conn.commit()
    except psycopg2.errors.DuplicateObject:
        conn.rollback()  # constraint already exists from a previous run — fine
    except Exception as e:
        conn.rollback()
        print(f"Could not add unique constraint on payments.order_id: {e}")

    cur.execute(
        """
        INSERT INTO users (fullname, username, password, role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (username) DO NOTHING
        """,
        ('System Administrator', 'admin', generate_password_hash('admin123'), 'ADMIN')
    )

    conn.commit()
    cur.close()
    conn.close()


def log_payment(conn, order_id, method, amount, status, reference=None):
    """Keeps ONE row per order_id up to date — inserts it the first time,
    and updates method/amount/status/reference/created_at in place on every
    later call for the same order (a retry, a status change, a cash
    settlement), instead of accumulating a new row per event. Doesn't
    commit — the caller commits alongside its own orders UPDATE so both
    happen atomically."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO payments(order_id, method, amount, status, reference)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (order_id) DO UPDATE SET
            method = EXCLUDED.method,
            amount = EXCLUDED.amount,
            status = EXCLUDED.status,
            reference = EXCLUDED.reference,
            created_at = CURRENT_TIMESTAMP
        """,
        (order_id, method, amount, status, reference)
    )
    cur.close()


# =====================================
# AUTH ROUTES
# =====================================

@app.route('/')
def home():
    return render_template("index.html")


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        username = data.get('username', '')
        password = data.get('password', '')

        conn = get_db()
        cur = dict_cursor(conn)
        cur.execute("SELECT * FROM users WHERE username=%s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and check_password_hash(user['password'], password):
            session.permanent = True
            session['user_id'] = user['id']
            session['fullname'] = user['fullname']
            session['role'] = user['role']
            session['phone'] = user['phone']
            return jsonify({"success": True, "role": user['role']})

        return jsonify({"success": False, "message": "Invalid username or password"})

    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        data = request.get_json()

        fullname = data.get('fullname')
        username = data.get('username')
        password = data.get('password')
        phone = data.get('phone')
        role = data.get('role', 'CUSTOMER')

        if not fullname or not username or not password or not phone:
            return jsonify({"success": False, "message": "Missing required fields"})

        conn = get_db()
        cur = conn.cursor()
        try:
            hashed_password = generate_password_hash(password)
            cur.execute(
                "INSERT INTO users (fullname, username, password, role, phone) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (fullname, username, hashed_password, role, phone)
            )
            new_user_id = cur.fetchone()[0]
            conn.commit()

            # Let any open admin dashboard add this account to its Users
            # table live, instead of only showing up after a refresh.
            socketio.emit(
                "new_user",
                {"id": new_user_id, "fullname": fullname, "username": username, "role": role},
                room="admins",
            )

            return jsonify({"success": True, "message": "Account created successfully"})
        except psycopg2.errors.UniqueViolation:
            conn.rollback()
            return jsonify({"success": False, "message": "Username already exists"})
        except Exception as e:
            conn.rollback()
            return jsonify({"success": False, "message": str(e)})
        finally:
            cur.close()
            conn.close()

    return render_template('register.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# =====================================
# CUSTOMER DASHBOARD
# =====================================

@app.route('/customer')
@login_required(role="CUSTOMER")
def customer_dashboard():
    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute(
        """
        SELECT id, customer, phone, service, status, payment_status, kg, amount
        FROM orders
        WHERE user_id=%s
        ORDER BY id DESC
        """,
        (session['user_id'],)
    )
    orders = cur.fetchall()
    cur.close()
    conn.close()
    return render_template("customer_dashboard.html", orders=orders, fullname=session.get('fullname'))


# =====================================
# ADMIN DASHBOARD
# =====================================

@app.route('/admin')
@login_required(role="ADMIN")
def admin_dashboard():
    conn = get_db()
    cur = dict_cursor(conn)

    cur.execute(
        """
        SELECT id, customer, phone, service, status, payment_status, kg, amount
        FROM orders
        ORDER BY id DESC
        """
    )
    orders = cur.fetchall()

    cur.execute("SELECT id, fullname, username, role FROM users")
    users = cur.fetchall()

    # Recent payment history — attempts, successes, failures, cod picks,
    # cash settlements — joined with customer name for readability.
    cur.execute(
        """
        SELECT p.id, p.order_id, p.method, p.amount, p.status, p.reference, p.created_at,
               o.customer
        FROM payments p
        LEFT JOIN orders o ON o.id = p.order_id
        ORDER BY p.id DESC
        LIMIT 50
        """
    )
    payments = cur.fetchall()

    cur.close()
    conn.close()
    return render_template(
        "admin_dashboard.html",
        orders=orders,
        users=users,
        payments=payments,
        fullname=session.get('fullname'),
    )


# =====================================
# ORDER API
# =====================================

@app.route('/orders', methods=['GET'])
@login_required(api=True)
def get_orders():
    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM orders ORDER BY id DESC")
    orders = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(orders)


@app.route('/orders_data')
def orders_data():
    if 'user_id' not in session:
        return jsonify([])

    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM orders")
    orders = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify(orders)


@app.route('/add_order', methods=['POST'])
@login_required(api=True)
def add_order():
    try:
        data = request.get_json() or {}
        service = data.get("service")
        if not service:
            return jsonify({"success": False, "message": "Missing service"})

        # Name and phone come from the account, not the request — the
        # customer already gave these once at registration, so they never
        # have to retype them for every order.
        customer = session.get('fullname')
        phone = session.get('phone')

        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO orders(user_id, customer, phone, service, status)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
            """,
            (session['user_id'], customer, phone, service, "Pending")
        )
        new_order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()

        order_payload = {
            "id": new_order_id,
            "customer": customer,
            "phone": phone,
            "service": service,
            "status": "Pending"
        }
        socketio.emit("new_order", order_payload, room=str(session['user_id']))
        socketio.emit("new_order", order_payload, room="admins")

        return jsonify({"success": True, "message": "Order added successfully"})

    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/complete/<int:order_id>', methods=['POST'])
@login_required(role="ADMIN", api=True)
def complete_order(order_id):
    """Marks an order Completed. The admin supplies the weight washed (kg);
    the amount to charge is computed here from PRICE_PER_KG."""
    data = request.get_json(silent=True) or {}
    try:
        kg = float(data.get("kg"))
    except (TypeError, ValueError):
        kg = None

    if not kg or kg <= 0:
        return jsonify({"success": False, "message": "Enter the total weight in kg"}), 400

    amount = round(kg * PRICE_PER_KG)

    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT user_id, customer, phone FROM orders WHERE id=%s", (order_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Order not found"}), 404

    cur.execute(
        "UPDATE orders SET status='Completed', kg=%s, amount=%s WHERE id=%s",
        (kg, amount, order_id)
    )
    conn.commit()
    cur.close()
    conn.close()

    sms_sent = send_sms(
        row["phone"],
        f"Hi {row['customer']}, your laundry order #{order_id} is ready for "
        f"pickup! Amount due: KES {amount}. Pay now at {SITE_URL}/customer"
    )

    payload = {
        "id": order_id,
        "customer": row["customer"],
        "phone": row["phone"],
        "kg": kg,
        "amount": amount,
    }

    if row["user_id"] is not None:
        socketio.emit("order_completed", payload, room=str(row["user_id"]))
    socketio.emit("order_completed", payload, room="admins")

    return jsonify({"success": True, "kg": kg, "amount": amount, "sms_sent": sms_sent})


@app.route('/delete/<int:order_id>', methods=['DELETE'])
@login_required(role="ADMIN", api=True)
def delete_order(order_id):
    try:
        conn = get_db()
        cur = dict_cursor(conn)
        cur.execute("SELECT user_id FROM orders WHERE id=%s", (order_id,))
        row = cur.fetchone()
        owner_id = row["user_id"] if row else None

        cur.execute("DELETE FROM orders WHERE id=%s", (order_id,))
        conn.commit()
        cur.close()
        conn.close()

        if owner_id is not None:
            socketio.emit("order_deleted", {"id": order_id}, room=str(owner_id))
        socketio.emit("order_deleted", {"id": order_id}, room="admins")

        return jsonify({"success": True, "message": "Order deleted"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/clear_orders', methods=['DELETE'])
@login_required(role="ADMIN", api=True)
def clear_orders():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM orders")
        conn.commit()
        cur.close()
        conn.close()
        socketio.emit("orders_cleared", {})
        return jsonify({"success": True, "message": "All orders cleared"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


@app.route('/delete_user/<int:user_id>', methods=['DELETE'])
@login_required(role="ADMIN", api=True)
def delete_user(user_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM users WHERE id=%s", (user_id,))
        conn.commit()
        cur.close()
        conn.close()

        socketio.emit("user_deleted", {"id": user_id})

        return jsonify({"success": True, "message": "User deleted successfully"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# =====================================
# PAYMENT API (M-PESA STK PUSH)
# =====================================

@app.route('/stk_push', methods=['POST'])
@login_required(api=True)
def stk_push():
    """Called from the 'Pay now' button. The client sends only order_id —
    phone and amount are read from the order row itself."""
    missing = [
        name for name, value in {
            "MPESA_CONSUMER_KEY": MPESA_CONSUMER_KEY,
            "MPESA_CONSUMER_SECRET": MPESA_CONSUMER_SECRET,
            "MPESA_SHORTCODE": MPESA_SHORTCODE,
            "MPESA_PASSKEY": MPESA_PASSKEY,
            "MPESA_CALLBACK_URL": MPESA_CALLBACK_URL,
        }.items() if not value
    ]
    if missing:
        return jsonify({
            "success": False,
            "message": f"M-Pesa is not configured: missing {', '.join(missing)} in your .env file"
        }), 500

    data = request.get_json() or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"success": False, "message": "Missing order_id"}), 400

    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Order not found"}), 404

    if session.get('role') != 'ADMIN' and order['user_id'] != session['user_id']:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Not authorized"}), 403

    if order['status'] != 'Completed' or not order['amount']:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "This order has no amount to pay yet"}), 400

    phone = normalize_phone(order['phone'])
    amount = int(order['amount'])

    cur.execute("UPDATE orders SET payment_status='pending' WHERE id=%s", (order_id,))
    log_payment(conn, order_id, 'mpesa', amount, 'pending')
    conn.commit()

    pending_payload = {
        "order_id": order_id,
        "customer": order['customer'],
        "method": "mpesa",
        "amount": amount,
        "status": "pending",
        "reference": None,
        "created_at": datetime.now().isoformat(),
    }
    if order['user_id'] is not None:
        socketio.emit("payment_status", pending_payload, room=str(order['user_id']))
    socketio.emit("payment_status", pending_payload, room="admins")

    try:
        token = mpesa_access_token()
    except requests.RequestException:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Could not reach M-Pesa. Try again."}), 502

    password, timestamp = mpesa_password_and_timestamp()

    payload = {
        "BusinessShortCode": MPESA_SHORTCODE,
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": amount,
        "PartyA": phone,
        "PartyB": MPESA_SHORTCODE,
        "PhoneNumber": phone,
        "CallBackURL": MPESA_CALLBACK_URL,
        "AccountReference": f"Order{order_id}",
        "TransactionDesc": "Laundry order payment",
    }

    try:
        r = requests.post(
            f"{MPESA_BASE_URL}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        result = r.json()
    except requests.RequestException:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Could not reach M-Pesa. Try again."}), 502

    if result.get("ResponseCode") == "0":
        cur.execute(
            "UPDATE orders SET checkout_request_id=%s, payment_method='mpesa' WHERE id=%s",
            (result["CheckoutRequestID"], order_id)
        )
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"success": True, "message": "Prompt sent"})

    cur.close()
    conn.close()
    return jsonify({
        "success": False,
        "message": result.get("errorMessage", "Could not start the payment")
    }), 400


@app.route("/callback", methods=["POST"])
def callback():
    try:
        data = request.get_json()

        print("=" * 60)
        print("CALLBACK RECEIVED")
        print(data)
        print("=" * 60)

        callback_data = data["Body"]["stkCallback"]
        checkout_request_id = callback_data["CheckoutRequestID"]
        result_code = int(callback_data["ResultCode"])
        result_desc = callback_data["ResultDesc"]

        conn = get_db()
        cur = dict_cursor(conn)
        cur.execute(
            "SELECT id, user_id, customer, amount FROM orders WHERE checkout_request_id=%s",
            (checkout_request_id,)
        )
        order = cur.fetchone()

        if not order:
            cur.close()
            conn.close()
            print(f"No order found for checkout id {checkout_request_id}")
            return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

        order_id = order["id"]
        user_id = order["user_id"]

        # ====================================
        # SUCCESSFUL PAYMENT
        # ====================================
        if result_code == 0:
            receipt = ""
            metadata = callback_data.get("CallbackMetadata", {}).get("Item", [])
            for item in metadata:
                if item.get("Name") == "MpesaReceiptNumber":
                    receipt = item.get("Value")
                # Amount is intentionally NOT taken from here — it was
                # already fixed by the admin's kg entry at complete time.

            cur.execute(
                "UPDATE orders SET payment_status='paid', mpesa_receipt=%s WHERE id=%s",
                (receipt, order_id)
            )
            log_payment(conn, order_id, 'mpesa', order['amount'], 'paid', receipt)
            conn.commit()
            cur.close()
            conn.close()

            print(f"PAYMENT SUCCESS Order #{order_id} Receipt={receipt}")

            payload = {
                "order_id": order_id,
                "customer": order['customer'],
                "method": "mpesa",
                "amount": order['amount'],
                "status": "paid",
                "reference": receipt,
                "created_at": datetime.now().isoformat(),
            }
            socketio.emit("payment_status", payload, room=str(user_id))
            socketio.emit("payment_status", payload, room="admins")

        # ====================================
        # FAILED PAYMENT
        # ====================================
        else:
            cur.execute("UPDATE orders SET payment_status='failed' WHERE id=%s", (order_id,))
            log_payment(conn, order_id, 'mpesa', order['amount'], 'failed')
            conn.commit()
            cur.close()
            conn.close()

            print(f"PAYMENT FAILED Order #{order_id}: {result_desc}")

            payload = {
                "order_id": order_id,
                "customer": order['customer'],
                "method": "mpesa",
                "amount": order['amount'],
                "status": "failed",
                "reference": None,
                "reason": result_desc,
                "created_at": datetime.now().isoformat(),
            }
            socketio.emit("payment_status", payload, room=str(user_id))
            socketio.emit("payment_status", payload, room="admins")

        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})

    except Exception as e:
        print("CALLBACK ERROR")
        print(str(e))
        return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


@app.route('/payment_method', methods=['POST'])
@login_required(api=True)
def payment_method():
    """Called from the 'Pay after delivery' button. Persists its own
    payment_status ('cod') — see /mark_paid below for how this later
    becomes 'paid' once cash is actually collected."""
    data = request.get_json() or {}
    order_id = data.get("order_id")
    method = data.get("method")

    if not order_id or not method:
        return jsonify({"success": False, "message": "Missing order_id or method"}), 400

    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Order not found"}), 404

    if session.get('role') != 'ADMIN' and order['user_id'] != session['user_id']:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Not authorized"}), 403

    cur.execute(
        "UPDATE orders SET payment_method=%s, payment_status='cod' WHERE id=%s",
        (method, order_id)
    )
    log_payment(conn, order_id, method, order['amount'] or 0, 'cod')
    conn.commit()
    owner_id = order['user_id']
    cur.close()
    conn.close()

    payload = {
        "order_id": order_id,
        "customer": order['customer'],
        "method": method,
        "amount": order['amount'] or 0,
        "status": "cod",
        "reference": None,
        "created_at": datetime.now().isoformat(),
    }
    if owner_id is not None:
        socketio.emit("payment_status", payload, room=str(owner_id))
    socketio.emit("payment_status", payload, room="admins")

    return jsonify({"success": True})


@app.route('/mark_paid/<int:order_id>', methods=['POST'])
@login_required(role="ADMIN", api=True)
def mark_paid(order_id):
    """Settles a cod (or otherwise unpaid) order once payment is actually
    collected — e.g. cash handed over at delivery. This is what turns
    'Pay on delivery' into 'Paid' instead of it staying that way forever."""
    conn = get_db()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE id=%s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Order not found"}), 404

    if order['payment_status'] == 'paid':
        cur.close()
        conn.close()
        return jsonify({"success": False, "message": "Already marked paid"}), 400

    amount = order['amount'] or 0

    cur.execute(
        "UPDATE orders SET payment_status='paid', payment_method='cash' WHERE id=%s",
        (order_id,)
    )
    log_payment(conn, order_id, 'cash', amount, 'paid')
    conn.commit()
    owner_id = order['user_id']
    cur.close()
    conn.close()

    payload = {
        "order_id": order_id,
        "customer": order['customer'],
        "method": "cash",
        "amount": amount,
        "status": "paid",
        "reference": None,
        "created_at": datetime.now().isoformat(),
    }
    if owner_id is not None:
        socketio.emit("payment_status", payload, room=str(owner_id))
    socketio.emit("payment_status", payload, room="admins")

    return jsonify({"success": True})


@socketio.on('connect')
def connect():
    if 'user_id' in session:
        join_room(str(session['user_id']))
        if session.get('role') == 'ADMIN':
            join_room('admins')


# =====================================
# SERVER START
# =====================================

if __name__ == '__main__':
    init_db()
    print("Laundry Management System Started")

    socketio.run(
        app,
        host="0.0.0.0",
        port=5001,
        debug=True,
        use_reloader=False  # prevents the dev server from restarting/reloading on every file save
    )
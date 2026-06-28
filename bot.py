"""
Samuga Travels Bot v1.1
Multi-tenant speedboat booking platform for the Maldives.
Single-file | asyncpg | Railway + PostgreSQL | Cloudinary
"""

import os, io, logging, asyncio, json, random, string
import cloudinary, cloudinary.uploader, requests
import asyncpg
from datetime import datetime
from decimal import Decimal
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN        = os.environ.get("BOT_TOKEN", "8973602844:AAGXMdvXqNPnTBWZGJNtJLb5ZKcMvjBgGE")
DATABASE_URL     = os.environ.get("DATABASE_URL", "")
ADMIN_GROUP_ID   = int(os.environ.get("ADMIN_GROUP_ID",  "-1004397030483"))
ADMIN_THREAD_ID  = int(os.environ.get("ADMIN_THREAD_ID", "2"))
GENERAL_THREAD_ID= int(os.environ.get("GENERAL_THREAD_ID","1"))
CLOUDINARY_CLOUD = os.environ.get("CLOUDINARY_CLOUD", "dfhj3clbh")
CLOUDINARY_KEY   = os.environ.get("CLOUDINARY_KEY",   "324844414354471")
CLOUDINARY_SECRET= os.environ.get("CLOUDINARY_SECRET","F4cmCOwLzIcSyXBhKZFzQDHevOk")

cloudinary.config(cloud_name=CLOUDINARY_CLOUD, api_key=CLOUDINARY_KEY, api_secret=CLOUDINARY_SECRET)

# ── STATES ────────────────────────────────────────────────────────────────────
OP_IDLE="op_idle"; OP_AWAIT_BUSINESS_NAME="op_await_business_name"
OP_AWAIT_LOGO="op_await_logo"; OP_AWAIT_BOAT_NAME="op_await_boat_name"
OP_AWAIT_SEATS="op_await_seats"; OP_AWAIT_TYPE="op_await_type"
OP_AWAIT_ROUTES="op_await_routes"; OP_AWAIT_OWNER_NAME="op_await_owner_name"
OP_AWAIT_OWNER_CONTACT="op_await_owner_contact"; OP_AWAIT_OWNER_ID_PHOTO="op_await_owner_id_photo"
OP_AWAIT_BML_ACCOUNT="op_await_bml_account"; OP_AWAIT_MIB_ACCOUNT="op_await_mib_account"; OP_REGISTERED="op_registered"
OP_AWAIT_SCHEDULE_ROUTE="op_await_schedule_route"; OP_AWAIT_SCHEDULE_TIME="op_await_schedule_time"
OP_AWAIT_SCHEDULE_PRICE="op_await_schedule_price"; OP_AWAIT_SCHEDULE_SEATS="op_await_schedule_seats"
CX_IDLE="cx_idle"; CX_AWAIT_DATE="cx_await_date"
CX_AWAIT_CONTACT="cx_await_contact"
CX_AWAIT_PASSENGER_COUNT="cx_await_passenger_count"
CX_COLLECTING_PASSENGERS="cx_collecting_passengers"; CX_AWAIT_PAYMENT_SLIP="cx_await_payment_slip"
CX_BOOKING_COMPLETE="cx_booking_complete"

# ── DB POOL ───────────────────────────────────────────────────────────────────
_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not set! Add it in Railway Variables.")
        db_url = DATABASE_URL.replace("postgres://", "postgresql://")
        for attempt in range(5):
            try:
                _pool = await asyncpg.create_pool(db_url, min_size=1, max_size=10)
                logger.info("✅ Database pool created")
                break
            except Exception as e:
                logger.error(f"DB pool attempt {attempt+1} failed: {e}")
                if attempt < 4:
                    await asyncio.sleep(3)
                else:
                    raise
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS operators (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                telegram_username TEXT,
                business_name TEXT,
                boat_name TEXT,
                logo_url TEXT,
                seat_count INTEGER DEFAULT 0,
                boat_type TEXT DEFAULT 'ferry',
                routes TEXT[],
                owner_name TEXT,
                owner_contact TEXT,
                owner_id_photo_url TEXT,
                bml_account TEXT,
                payment_accounts TEXT DEFAULT '[]',
                status TEXT DEFAULT 'pending',
                is_recommended BOOLEAN DEFAULT FALSE,
                review_text TEXT,
                average_rating DECIMAL(3,2) DEFAULT 0,
                total_reviews INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schedules (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id) ON DELETE CASCADE,
                route_from TEXT NOT NULL,
                route_to TEXT NOT NULL,
                departure_time TEXT NOT NULL,
                price_per_seat DECIMAL(10,2) NOT NULL,
                total_seats INTEGER NOT NULL,
                available_seats INTEGER NOT NULL,
                is_active BOOLEAN DEFAULT TRUE,
                sched_stops TEXT DEFAULT '[]',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id SERIAL PRIMARY KEY,
                booking_ref TEXT UNIQUE NOT NULL,
                customer_telegram_id BIGINT NOT NULL,
                customer_name TEXT,
                operator_id INTEGER REFERENCES operators(id),
                schedule_id INTEGER REFERENCES schedules(id),
                travel_date DATE NOT NULL,
                passenger_count INTEGER NOT NULL,
                passengers TEXT DEFAULT '[]',
                total_amount DECIMAL(10,2) NOT NULL,
                status TEXT DEFAULT 'pending_payment',
                payment_slip_url TEXT,
                ticket_url TEXT,
                created_at TIMESTAMP DEFAULT NOW(),
                confirmed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                telegram_id BIGINT PRIMARY KEY,
                role TEXT DEFAULT 'customer',
                state TEXT DEFAULT 'cx_idle',
                temp_data TEXT DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id),
                customer_telegram_id BIGINT NOT NULL,
                booking_id INTEGER REFERENCES bookings(id),
                rating INTEGER CHECK (rating >= 1 AND rating <= 5),
                comment TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    logger.info("✅ Database initialized")

# ── DB HELPERS ────────────────────────────────────────────────────────────────
async def get_user_state(telegram_id: int) -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM user_states WHERE telegram_id = $1", telegram_id)
    if row:
        d = dict(row)
        d["temp_data"] = json.loads(d.get("temp_data") or "{}")
        return d
    return {"telegram_id": telegram_id, "role": "customer", "state": CX_IDLE, "temp_data": {}}

async def set_user_state(telegram_id: int, state: str, temp_data: dict = None, role: str = None):
    pool = await get_pool()
    td = json.dumps(temp_data or {})
    async with pool.acquire() as conn:
        if role:
            await conn.execute("""
                INSERT INTO user_states (telegram_id, state, temp_data, role)
                VALUES ($1,$2,$3,$4)
                ON CONFLICT (telegram_id) DO UPDATE
                SET state=$2, temp_data=$3, role=$4, updated_at=NOW()
            """, telegram_id, state, td, role)
        else:
            await conn.execute("""
                INSERT INTO user_states (telegram_id, state, temp_data)
                VALUES ($1,$2,$3)
                ON CONFLICT (telegram_id) DO UPDATE
                SET state=$2, temp_data=$3, updated_at=NOW()
            """, telegram_id, state, td)

async def update_temp_key(telegram_id: int, key: str, value):
    sd = await get_user_state(telegram_id)
    temp = sd.get("temp_data", {}) or {}
    temp[key] = value
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE user_states SET temp_data=$1, updated_at=NOW() WHERE telegram_id=$2",
            json.dumps(temp), telegram_id)

async def get_operator(telegram_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM operators WHERE telegram_id=$1", telegram_id)
    return dict(row) if row else None

def gen_ref():
    ts = datetime.now().strftime("%y%m%d")
    rand = ''.join(random.choices(string.digits, k=4))
    return f"ST-{ts}-{rand}"

# ── CLOUDINARY ────────────────────────────────────────────────────────────────
async def upload_image(file_bytes: bytes, folder: str, filename: str) -> str:
    result = cloudinary.uploader.upload(
        file_bytes, folder=f"samuga_travels/{folder}",
        public_id=filename, overwrite=True, resource_type="image")
    return result["secure_url"]

# ── PDF TICKET ────────────────────────────────────────────────────────────────
def generate_ticket_pdf(booking: dict, operator: dict, schedule: dict) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    story = []

    title_s = ParagraphStyle('t', parent=styles['Title'], fontSize=20,
                              textColor=colors.HexColor('#1a3a5c'), alignment=TA_CENTER, spaceAfter=4)
    sub_s   = ParagraphStyle('s', parent=styles['Normal'], fontSize=9,
                              textColor=colors.HexColor('#666'), alignment=TA_CENTER, spaceAfter=6)

    if operator.get("logo_url"):
        try:
            resp = requests.get(operator["logo_url"], timeout=5)
            img = RLImage(io.BytesIO(resp.content), width=45*mm, height=45*mm)
            img.hAlign = 'CENTER'
            story.append(img)
            story.append(Spacer(1, 3*mm))
        except: pass

    story.append(Paragraph(operator.get("business_name","Speedboat Service"), title_s))
    story.append(Paragraph("Powered by Samuga Travels 🌊", sub_s))
    story.append(Spacer(1, 5*mm))

    data = [
        ["🎫 BOOKING REF",  booking["booking_ref"]],
        ["🚤 BOAT",         operator.get("boat_name","N/A")],
        ["📍 ROUTE",        f"{schedule['route_from']} → {schedule['route_to']}"],
        ["📅 DATE",         str(booking["travel_date"])],
        ["⏰ DEPARTURE",    schedule["departure_time"]],
        ["👥 PASSENGERS",   str(booking["passenger_count"])],
        ["💰 TOTAL PAID",   f"MVR {booking['total_amount']}"],
    ]
    t = Table(data, colWidths=[70*mm, 90*mm])
    t.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,0), colors.HexColor('#1a3a5c')),
        ('TEXTCOLOR',(0,0),(-1,0), colors.white),
        ('FONTNAME',(0,0),(-1,-1),'Helvetica'),
        ('FONTSIZE',(0,0),(-1,-1),10),
        ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f0f4f8'),colors.white]),
        ('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#cde')),
        ('PADDING',(0,0),(-1,-1),8),
    ]))
    story.append(t)
    story.append(Spacer(1,5*mm))

    passengers = booking.get("passengers",[])
    if isinstance(passengers, str):
        passengers = json.loads(passengers)
    if passengers:
        story.append(Paragraph("Passenger Details", ParagraphStyle('ph',parent=styles['Heading2'],
                                fontSize=12,textColor=colors.HexColor('#1a3a5c'))))
        pd = [["#","Full Name","ID / Passport"]]
        for i,p in enumerate(passengers,1):
            pd.append([str(i), p.get("name",""), p.get("id_number","")])
        pt = Table(pd, colWidths=[10*mm,80*mm,70*mm])
        pt.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,0),colors.HexColor('#2e86ab')),
            ('TEXTCOLOR',(0,0),(-1,0),colors.white),
            ('FONTNAME',(0,0),(-1,0),'Helvetica-Bold'),
            ('FONTSIZE',(0,0),(-1,-1),9),
            ('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.HexColor('#f7fbff'),colors.white]),
            ('GRID',(0,0),(-1,-1),0.5,colors.HexColor('#c0d8f0')),
            ('PADDING',(0,0),(-1,-1),6),
        ]))
        story.append(pt)
        story.append(Spacer(1,5*mm))

    story.append(Paragraph("✅ Present this ticket when boarding.",
        ParagraphStyle('f1',parent=styles['Normal'],fontSize=9,
                       textColor=colors.HexColor('#444'),alignment=TA_CENTER)))
    story.append(Paragraph("Samuga Travels — Safe travels! 🌊",
        ParagraphStyle('f2',parent=styles['Normal'],fontSize=8,
                       textColor=colors.HexColor('#888'),alignment=TA_CENTER)))
    doc.build(story)
    return buf.getvalue()

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def main_kb(role="customer"):
    if role == "operator":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Profile",       callback_data="op_profile"),
             InlineKeyboardButton("🗓️ Add Schedule",     callback_data="op_schedules")],
            [InlineKeyboardButton("📦 Pending Bookings", callback_data="op_bookings"),
             InlineKeyboardButton("✏️ Edit Info",        callback_data="op_edit")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Boats",           callback_data="cx_search"),
         InlineKeyboardButton("📋 My Bookings",            callback_data="cx_my_bookings")],
        [InlineKeyboardButton("🤝 Register as Operator",   callback_data="register_operator")],
    ])

def boat_type_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛴️ Ferry Service",  callback_data="type_ferry")],
        [InlineKeyboardButton("🛥️ Private Hire",   callback_data="type_private")],
    ])

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd = await get_user_state(user.id)
    role = sd.get("role","customer")
    if role == "operator":
        op = await get_operator(user.id)
        if op and op.get("status") == "approved":
            role = "operator"
        else:
            role = "customer"
    await update.message.reply_text(
        f"🌊 *Welcome to Samuga Travels!*\n\nHi *{user.first_name}*! The Maldives' smartest speedboat booking platform.\n\nWhat would you like to do?",
        parse_mode="Markdown", reply_markup=main_kb(role))

async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd = await get_user_state(user.id)
    role = sd.get("role","customer")
    await set_user_state(user.id, CX_IDLE if role != "operator" else OP_IDLE, {})
    await update.message.reply_text("❌ Cancelled. Back to main menu.", reply_markup=main_kb(role))

async def cmd_register(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_op_reg(update, ctx)

async def cmd_recommend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /recommend <telegram_id> <review text>")
        return
    op_tid = int(args[0])
    review_text = " ".join(args[1:])
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE operators SET is_recommended=TRUE, review_text=$1 WHERE telegram_id=$2 RETURNING business_name, id",
            review_text, op_tid)
    if row:
        await update.message.reply_text(f"✅ *{row['business_name']}* is now Recommended!", parse_mode="Markdown")
        await ctx.bot.send_message(op_tid,
            f"🌟 *Congratulations!*\n\nYour business has been marked *Recommended by Samuga Travels!*\n\n💬 _{review_text}_",
            parse_mode="Markdown")

# ── OPERATOR REGISTRATION ─────────────────────────────────────────────────────
async def start_op_reg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user if query else update.effective_user
    msg  = query.message   if query else update.message

    existing = await get_operator(user.id)
    if existing:
        s = existing.get("status")
        if s == "approved":
            await set_user_state(user.id, OP_IDLE, {}, role="operator")
            await msg.reply_text("✅ You're already a verified operator! Use /start to manage.")
        elif s == "pending":
            await msg.reply_text("⏳ Your application is under review. We'll notify you once approved.")
        else:
            await msg.reply_text("❌ Your application was rejected. Contact @SamugaTravels for support.")
        return

    await set_user_state(user.id, OP_AWAIT_BUSINESS_NAME, {}, role="operator_pending")
    await msg.reply_text(
        "🚤 *Operator Registration — Samuga Travels*\n\n"
        "*Step 1 of 9:* What is your *business/company name*?\n\n_Example: Thoddoo Express Travels_",
        parse_mode="Markdown")

# ── MESSAGE HANDLER ───────────────────────────────────────────────────────────
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd   = await get_user_state(user.id)
    state= sd.get("state", CX_IDLE)
    temp = sd.get("temp_data", {}) or {}
    text = (update.message.text or "").strip()

    # ── OPERATOR REG FLOW ─────────────────────────────────────────────────────
    if state == OP_AWAIT_BUSINESS_NAME:
        await set_user_state(user.id, OP_AWAIT_BOAT_NAME, {**temp, "business_name": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 2 of 9:* What is your *boat name*?\n\n_Example: Ocean Star_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BOAT_NAME:
        await set_user_state(user.id, OP_AWAIT_SEATS, {**temp, "boat_name": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 3 of 9:* How many *seats* does your boat have?\n\n_Enter a number, e.g. 20_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SEATS:
        if not text.isdigit():
            await update.message.reply_text("⚠️ Please enter a valid number.")
            return
        await set_user_state(user.id, OP_AWAIT_TYPE, {**temp, "seat_count": int(text)})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 4 of 9:* What type of service?",
            parse_mode="Markdown", reply_markup=boat_type_kb())

    elif state == OP_AWAIT_ROUTES:
        stops = [s.strip() for s in text.split(",") if s.strip()]
        if len(stops) < 2:
            await update.message.reply_text(
                "⚠️ Enter at least 2 stops separated by commas.\n\n"
                "_Example: `Male, Dhigurah, Thoddoo, Dhagethi`_",
                parse_mode="Markdown")
            return
        route_display = " → ".join(stops)
        await set_user_state(user.id, OP_AWAIT_OWNER_NAME, {**temp, "routes": stops, "route_display": route_display})
        await update.message.reply_text(
            f"✅ Route saved!\n\n📍 *{route_display}*\n\n*Step 6 of 9:* What is the *owner's full name*?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_NAME:
        await set_user_state(user.id, OP_AWAIT_OWNER_CONTACT, {**temp, "owner_name": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 7 of 9:* Owner's *contact number*?\n\n_Example: 7771234_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_CONTACT:
        await set_user_state(user.id, OP_AWAIT_OWNER_ID_PHOTO, {**temp, "owner_contact": text})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 8 of 9:* Please upload a *photo of the owner's ID card or passport*.\n\n"
            "_For verification purposes only — kept secure._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BML_ACCOUNT:
        parts = text.strip().split(" ", 1)
        acct_num = parts[0].strip()
        acct_name = parts[1].strip() if len(parts) > 1 else ""
        bml_entry = f"{acct_num}|{acct_name}" if acct_name else acct_num
        await set_user_state(user.id, OP_AWAIT_MIB_ACCOUNT, {**temp, "bml_account": bml_entry})
        await update.message.reply_text(
            "✅ *BML account saved!*\n\n"
            "Do you also have an *MIB (Maldives Islamic Bank)* account?\n\n"
            "_Enter number and account name e.g:_\n`90101480050561001 Samuga Travels`\n\n"
            "_Or type_ *skip* _if not._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_MIB_ACCOUNT:
        if text.strip().lower() == "skip":
            mib_entry = ""
        else:
            parts = text.strip().split(" ", 1)
            acct_num = parts[0].strip()
            acct_name = parts[1].strip() if len(parts) > 1 else ""
            mib_entry = f"{acct_num}|{acct_name}" if acct_name else acct_num
        final_temp = {**temp, "mib_account": mib_entry}
        await save_operator(user, final_temp)
        await notify_admin_new_op(ctx, user, final_temp)
        await set_user_state(user.id, OP_REGISTERED, {})
        await update.message.reply_text(
            "🎉 *Registration Complete!*\n\n"
            "Your application has been submitted to Samuga Travels for review.\n\n"
            "⏳ We\'ll verify your details and notify you here within 24 hours. Thank you! 🌊",
            parse_mode="Markdown")

    # ── SCHEDULE FLOW ─────────────────────────────────────────────────────────
    elif state == OP_AWAIT_SCHEDULE_ROUTE:
        stops = [s.strip().title() for s in text.split(",") if s.strip()]
        if len(stops) < 2:
            # Also support "Male to Thoddoo" format as 2-stop
            parts = [p.strip().title() for p in text.split("to", 1)]
            if len(parts) == 2 and parts[0] and parts[1]:
                stops = parts
            else:
                await update.message.reply_text(
                    "⚠️ Enter stops comma-separated or use 'from to destination'\n\n"
                    "_Single route: `Male, Thoddoo`_\n"
                    "_Multi-stop: `Male, Dhigurah, Thoddoo, Dhagethi`_",
                    parse_mode="Markdown")
                return
        route_display = " → ".join(stops)
        sched_from = stops[0]
        sched_to = stops[-1]
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_TIME,
                             {**temp, "sched_from": sched_from, "sched_to": sched_to,
                              "sched_stops": stops, "route_display": route_display})
        await update.message.reply_text(
            f"✅ Route saved!\n\n📍 *{route_display}*\n\nWhat is the *departure time*?\n\n_Example: 04:00 PM_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_TIME:
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_PRICE, {**temp, "sched_time": text})
        await update.message.reply_text(
            "✅ Time saved!\n\nWhat is the *price per seat* (MVR)?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_PRICE:
        try:
            price = float(text)
        except ValueError:
            await update.message.reply_text("⚠️ Enter a valid price e.g. `535`", parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_SEATS, {**temp, "sched_price": price})
        await update.message.reply_text("✅ Price saved!\n\nHow many *available seats* for this schedule?",
                                        parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_SEATS:
        if not text.isdigit():
            await update.message.reply_text("⚠️ Enter a valid number.")
            return
        seats = int(text)
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        op  = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            import json as _ji
            await conn.execute("""
                ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'
            """)
            await conn.execute("""
                INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                       price_per_seat, total_seats, available_seats, sched_stops)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                t2.get("sched_time"), t2.get("sched_price"), seats, seats,
                _ji.dumps(t2.get("sched_stops", [])))
        await set_user_state(user.id, OP_IDLE, {})
        await update.message.reply_text(
            f"✅ *Schedule Added!*\n\n"
            f"📍 {t2.get('sched_from')} → {t2.get('sched_to')}\n"
            f"⏰ {t2.get('sched_time')}\n💰 MVR {t2.get('sched_price')}/seat\n👥 {seats} seats",
            parse_mode="Markdown", reply_markup=main_kb("operator"))

    # ── CUSTOMER FLOW ─────────────────────────────────────────────────────────
    elif state == CX_AWAIT_DATE:
        try:
            travel_date = datetime.strptime(text, "%d-%m-%Y").date()
        except ValueError:
            await update.message.reply_text(
                "⚠️ Invalid date. Use DD-MM-YYYY\n\nExample: `30-06-2026`", parse_mode="Markdown")
            return
        if travel_date < datetime.now().date():
            await update.message.reply_text("⚠️ Date cannot be in the past.")
            return

        route_from = temp.get("route_from","")
        route_to   = temp.get("route_to","")
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT s.*, s.sched_stops, o.id as operator_id, o.business_name, o.boat_name, o.logo_url,
                       o.is_recommended, o.average_rating, o.total_reviews,
                       o.review_text, o.bml_account, o.payment_accounts, o.telegram_id as op_telegram_id
                FROM schedules s
                JOIN operators o ON s.operator_id = o.id
                WHERE LOWER(s.route_from) LIKE $1 AND LOWER(s.route_to) LIKE $2
                  AND o.status='approved' AND s.is_active=TRUE AND s.available_seats>0
                ORDER BY o.is_recommended DESC, s.departure_time ASC
            """, f"%{route_from.lower()}%", f"%{route_to.lower()}%")

        if not rows:
            await update.message.reply_text(
                f"😔 No boats found for *{route_from} → {route_to}* on *{text}*.\n\nTry a different date or route.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Search Again", callback_data="cx_search")]]))
            await set_user_state(user.id, CX_IDLE, {})
            return

        schedules = [dict(r) for r in rows]
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT,
                             {**temp, "travel_date": str(travel_date), "schedules": schedules})

        msg = f"🚢 *Available Boats — {route_from} → {route_to}*\n📅 *{text}*\n\n"
        buttons = []
        for i, s in enumerate(schedules):
            rating_val = float(s.get("average_rating") or 0)
            stars = "⭐" * int(rating_val) if rating_val else "No ratings yet"
            rec = "✨ *Recommended by Samuga Travels*\n" if s.get("is_recommended") else ""
            # Build stops line for multi-stop ferries
            import json as _j
            try:
                stops_list = _j.loads(s.get("sched_stops") or "[]")
                if stops_list and len(stops_list) > 2:
                    stops_line = "🛑 " + " → ".join(stops_list) + "\n"
                else:
                    stops_line = ""
            except Exception:
                stops_line = ""
            msg += (
                f"{'─'*30}\n"
                f"🚤 *{s['business_name']}* — _{s['boat_name']}_\n"
                f"{rec}"
                f"📍 {s['route_from']} → {s['route_to']}\n"
                f"{stops_line}"
                f"⏰ Departure: *{s['departure_time']}*\n"
                f"💺 Available: *{s['available_seats']} seats*\n"
                f"💰 Price: *MVR {s['price_per_seat']}/seat*\n"
                f"⭐ {stars} ({s.get('total_reviews',0)} reviews)\n"
            )
            if s.get("review_text"):
                msg += f"💬 _{s['review_text']}_\n"
            msg += "\n"
            buttons.append([InlineKeyboardButton(
                f"Book — {s['business_name']} ({s['departure_time']})",
                callback_data=f"book_sched_{i}")])

        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif state == CX_AWAIT_CONTACT:
        parts = text.split(",", 1)
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Format: `Full Name, Phone Number`\n\nExample: `Ahmed Ali, 7771234`", parse_mode="Markdown")
            return
        cx_name = parts[0].strip()
        cx_phone = parts[1].strip()
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT, {**temp, "cx_name": cx_name, "cx_phone": cx_phone})
        schedules = temp.get("schedules", [])
        idx = temp.get("selected_schedule_idx", 0)
        sel = schedules[idx] if schedules else {}
        await update.message.reply_text(
            f"✅ *{cx_name}* saved!\n\n"
            f"💺 How many seats would you like to book?\n_(Max 10, available: {sel.get('available_seats',0)})_",
            parse_mode="Markdown")

    elif state == CX_AWAIT_PASSENGER_COUNT:
        if not text.isdigit() or int(text) < 1:
            await update.message.reply_text("⚠️ Enter a valid number (1-10).")
            return
        count = int(text)
        if count > 10:
            await update.message.reply_text("⚠️ Maximum 10 seats per booking.")
            return
        schedules = temp.get("schedules", [])
        idx = temp.get("selected_schedule_idx", 0)
        selected = schedules[idx] if schedules else {}
        if count > int(selected.get("available_seats", 0)):
            await update.message.reply_text(f"⚠️ Only *{selected.get('available_seats')} seats* available.", parse_mode="Markdown")
            return
        await set_user_state(user.id, CX_COLLECTING_PASSENGERS,
                             {**temp, "passenger_count": count, "passengers_collected": [], "current_passenger": 1})
        await update.message.reply_text(
            f"👤 *Passenger 1 of {count}*\n\nEnter *Full Name* and *ID / Passport Number*:\n\n_Format: Ahmed Ali, A123456_",
            parse_mode="Markdown")

    elif state == CX_COLLECTING_PASSENGERS:
        parts = text.split(",", 1)
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Format: `Full Name, ID Number`\n\nExample: `Ahmed Ali, A123456`", parse_mode="Markdown")
            return
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        passengers = t2.get("passengers_collected", [])
        current    = t2.get("current_passenger", 1)
        total      = t2.get("passenger_count", 1)
        passengers.append({"name": parts[0].strip(), "id_number": parts[1].strip()})

        if current < total:
            await set_user_state(user.id, CX_COLLECTING_PASSENGERS,
                                 {**t2, "passengers_collected": passengers, "current_passenger": current+1})
            await update.message.reply_text(
                f"✅ Passenger {current} saved!\n\n"
                f"👤 *Passenger {current+1} of {total}*\n\nEnter *Full Name* and *ID / Passport Number*:\n\n_Format: Ahmed Ali, A123456_",
                parse_mode="Markdown")
        else:
            # All collected — show summary + payment
            sd3 = await get_user_state(user.id)
            t3  = sd3.get("temp_data", {}) or {}
            t3["passengers_collected"] = passengers
            schedules = t3.get("schedules", [])
            idx = t3.get("selected_schedule_idx", 0)
            sel = schedules[idx] if schedules else {}
            total_amt = float(sel.get("price_per_seat",0)) * total
            pax_lines = "\n".join([f"  {i+1}. {p['name']} ({p['id_number']})" for i,p in enumerate(passengers)])
            # Build payment accounts display
            import json as _json
            pay_str = ""
            try:
                accounts = _json.loads(sel.get("payment_accounts") or "[]")
                if accounts:
                    for acc in accounts:
                        pay_str += f"🏦 *{acc['bank']}:* `{acc['number']}`"
                        if acc.get("name"): pay_str += f" — {acc['name']}"
                        pay_str += "\n"
                else:
                    pay_str = f"🏦 *BML:* `{sel.get('bml_account','N/A')}`\n"
            except Exception:
                pay_str = f"🏦 *BML:* `{sel.get('bml_account','N/A')}`\n"

            summary = (
                f"📝 *Booking Summary*\n\n"
                f"👤 *Booker:* {t3.get('cx_name','N/A')} | 📞 {t3.get('cx_phone','N/A')}\n"
                f"🚤 *Operator:* {sel.get('business_name')}\n"
                f"🛥️ *Boat:* {sel.get('boat_name')}\n"
                f"📍 *Route:* {t3.get('route_from')} → {t3.get('route_to')}\n"
                f"📅 *Date:* {t3.get('travel_date')}\n"
                f"⏰ *Departure:* {sel.get('departure_time')}\n"
                f"👥 *Passengers ({total}):*\n{pax_lines}\n\n"
                f"💰 *Total:* MVR {total_amt:.2f}\n\n"
                f"{'─'*30}\n"
                f"💳 *Payment Details:*\n\n"
                f"{pay_str}"
                f"💰 Amount: *MVR {total_amt:.2f}*\n\n"
                f"👉 After transferring, *upload your payment screenshot here.*"
            )
            await set_user_state(user.id, CX_AWAIT_PAYMENT_SLIP,
                                 {**t3, "total_amount": str(total_amt), "passengers_collected": passengers})
            await update.message.reply_text(summary, parse_mode="Markdown")

    else:
        # Default — route search from text
        if " to " in text.lower():
            parts = text.lower().split(" to ", 1)
            rf = parts[0].strip().title()
            rt = parts[1].strip().title()
            await set_user_state(user.id, CX_AWAIT_DATE, {"route_from": rf, "route_to": rt})
            await update.message.reply_text(
                f"🔍 Searching: *{rf} → {rt}*\n\n📅 What is your *travel date*?\n\n_Format: DD-MM-YYYY (e.g. 30-06-2026)_",
                parse_mode="Markdown")
        else:
            sd2 = await get_user_state(user.id)
            role = sd2.get("role","customer")
            if role == "operator":
                op = await get_operator(user.id)
                role = "operator" if (op and op.get("status")=="approved") else "customer"
            await update.message.reply_text(
                "👋 Type a route like *Male to Thoddoo* to search, or use the menu.",
                parse_mode="Markdown", reply_markup=main_kb(role))

# ── PHOTO HANDLER ─────────────────────────────────────────────────────────────
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sd   = await get_user_state(user.id)
    state= sd.get("state", CX_IDLE)
    temp = sd.get("temp_data", {}) or {}

    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    file_bytes = bytes(await f.download_as_bytearray())

    if state == OP_AWAIT_LOGO:
        await update.message.reply_text("⏳ Uploading logo...")
        url = await upload_image(file_bytes, "logos", f"logo_{user.id}")
        await set_user_state(user.id, OP_AWAIT_ROUTES, {**temp, "logo_url": url})
        await update.message.reply_text(
            "✅ Logo uploaded!\n\n*Step 5 of 9:* Enter your *route with all stops in order*\n\n"
            "_For a ferry with multiple stops:_\n"
            "`Male, Dhigurah, Thoddoo, Dhagethi`\n\n"
            "_For a direct route:_\n"
            "`Male, Thoddoo`\n\n"
            "_Separate each stop with a comma in travel order._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_ID_PHOTO:
        await update.message.reply_text("⏳ Uploading ID securely...")
        url = await upload_image(file_bytes, "id_photos", f"id_{user.id}")
        await set_user_state(user.id, OP_AWAIT_BML_ACCOUNT, {**temp, "owner_id_photo_url": url})
        await update.message.reply_text(
            "✅ ID uploaded!\n\n*Step 9 of 10:* Your *BML bank account number and account name*?\n\n"
            "_Format: AccountNumber AccountName_\n_Example: 7770000234231 Samuga Art_",
            parse_mode="Markdown")

    elif state == CX_AWAIT_PAYMENT_SLIP:
        await update.message.reply_text("⏳ Processing your payment slip...")
        ref = gen_ref()
        url = await upload_image(file_bytes, "payment_slips", f"slip_{ref}")
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        schedules = t2.get("schedules", [])
        idx = t2.get("selected_schedule_idx", 0)
        sel = schedules[idx] if schedules else {}

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO bookings (booking_ref, customer_telegram_id, customer_name, operator_id, schedule_id,
                                      travel_date, passenger_count, passengers, total_amount,
                                      payment_slip_url, status)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending_confirmation')
                RETURNING id
            """, ref, user.id, f"{t2.get('cx_name','')} | {t2.get('cx_phone','')}",
                sel.get("operator_id"), sel.get("id"),
                t2.get("travel_date"), t2.get("passenger_count"),
                json.dumps(t2.get("passengers_collected",[])),
                t2.get("total_amount"), url)
        booking_id = row["id"]

        await set_user_state(user.id, CX_BOOKING_COMPLETE, {"booking_ref": ref, "booking_id": booking_id})
        await update.message.reply_text(
            f"✅ *Payment slip received!*\n\n"
            f"📋 Booking Ref: `{ref}`\n\n"
            f"Your booking is being reviewed by the operator. "
            f"You'll receive your ticket here within 5 minutes. 🌊",
            parse_mode="Markdown")

        await notify_operator_payment(ctx, booking_id, sel, t2, ref, user, photo.file_id)

    else:
        await update.message.reply_text("⚠️ Wasn't expecting an image. Use /start to go back.")

# ── CALLBACK HANDLER ──────────────────────────────────────────────────────────
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data
    sd   = await get_user_state(user.id)
    temp = sd.get("temp_data", {}) or {}

    if data == "register_operator":
        await start_op_reg(update, ctx)

    elif data == "cx_search":
        await set_user_state(user.id, CX_IDLE, {})
        await query.message.reply_text(
            "🔍 *Search for Boats*\n\nType your route:\n`Male to Thoddoo`\n`Thoddoo to Male`",
            parse_mode="Markdown")

    elif data == "cx_my_bookings":
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.*, o.business_name FROM bookings b
                JOIN operators o ON b.operator_id = o.id
                WHERE b.customer_telegram_id=$1 ORDER BY b.created_at DESC LIMIT 5
            """, user.id)
        if not rows:
            await query.message.reply_text("📋 No bookings yet.")
            return
        icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
        msg = "📋 *Your Recent Bookings:*\n\n"
        for b in rows:
            ic = icons.get(b["status"],"❓")
            msg += f"{ic} `{b['booking_ref']}` — {b['business_name']}\n   📅 {b['travel_date']} | MVR {b['total_amount']}\n\n"
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data.startswith("book_sched_"):
        idx = int(data.split("_")[-1])
        schedules = temp.get("schedules", [])
        if idx >= len(schedules):
            await query.message.reply_text("⚠️ Invalid selection.")
            return
        sel = schedules[idx]
        await set_user_state(user.id, CX_AWAIT_CONTACT, {**temp, "selected_schedule_idx": idx})
        await query.message.reply_text(
            f"✅ *{sel['business_name']}* selected!\n\n"
            f"⏰ {sel['departure_time']} | 💺 {sel['available_seats']} seats available\n\n"
            f"👤 *Your contact details:*\n\nEnter your *Full Name* and *Phone Number*:\n\n_Format: Ahmed Ali, 7771234_",
            parse_mode="Markdown")

    elif data.startswith("type_"):
        boat_type = data.split("_")[1]
        await set_user_state(user.id, OP_AWAIT_LOGO, {**temp, "boat_type": boat_type})
        await query.message.reply_text(
            f"✅ *{'Ferry' if boat_type=='ferry' else 'Private Hire'}* selected!\n\n"
            f"*Step 5 of 9:* Please upload your *boat/company logo*.",
            parse_mode="Markdown")

    elif data.startswith("approve_op_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET status='approved' WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            await set_user_state(row["telegram_id"], OP_IDLE, {}, role="operator")
            await ctx.bot.send_message(row["telegram_id"],
                f"🎉 *Congratulations!*\n\n*{row['business_name']}* has been *approved* by Samuga Travels!\n\n"
                f"You can now add schedules and receive bookings.\n\nUse /start to manage your account. 🌊",
                parse_mode="Markdown")
            await query.edit_message_text(f"✅ Operator *{row['business_name']}* approved!", parse_mode="Markdown")

    elif data.startswith("reject_op_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET status='rejected' WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            await ctx.bot.send_message(row["telegram_id"],
                f"❌ Your application for *{row['business_name']}* was not approved.\nContact @SamugaTravels for info.",
                parse_mode="Markdown")
            await query.edit_message_text(f"❌ Operator *{row['business_name']}* rejected.", parse_mode="Markdown")

    elif data.startswith("confirm_booking_"):
        booking_id = int(data.split("_")[-1])
        await do_confirm_booking(ctx, booking_id, query)

    elif data == "op_schedules":
        op = await get_operator(user.id)
        if not op or op.get("status") != "approved":
            await query.message.reply_text("⚠️ Account not yet approved.")
            return
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_ROUTE, {})
        await query.message.reply_text(
            "🗓️ *Add a New Schedule*\n\nEnter the route:\n_Format: Male to Thoddoo_",
            parse_mode="Markdown")

    elif data == "op_profile":
        op = await get_operator(user.id)
        if not op:
            await query.message.reply_text("⚠️ No operator profile found.")
            return
        routes = ", ".join(op.get("routes") or [])
        await query.message.reply_text(
            f"🚤 *Your Operator Profile*\n\n"
            f"🏢 *Business:* {op['business_name']}\n"
            f"🛥️ *Boat:* {op['boat_name']}\n"
            f"💺 *Seats:* {op['seat_count']}\n"
            f"📍 *Routes:* {routes}\n"
            f"📊 *Status:* {op['status'].upper()}\n"
            f"⭐ *Rating:* {op['average_rating']} ({op['total_reviews']} reviews)\n"
            f"✨ *Recommended:* {'Yes 🌟' if op['is_recommended'] else 'No'}\n",
            parse_mode="Markdown", reply_markup=main_kb("operator"))

    elif data == "op_bookings":
        op = await get_operator(user.id)
        if not op:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT b.*, s.route_from, s.route_to, s.departure_time
                FROM bookings b JOIN schedules s ON b.schedule_id=s.id
                WHERE b.operator_id=$1 AND b.status='pending_confirmation'
                ORDER BY b.created_at DESC LIMIT 10
            """, op["id"])
        if not rows:
            await query.message.reply_text("📦 No pending bookings.")
            return
        for b in rows:
            await query.message.reply_text(
                f"📦 *Pending Booking*\n\n🔖 `{b['booking_ref']}`\n"
                f"📍 {b['route_from']} → {b['route_to']}\n"
                f"📅 {b['travel_date']} @ {b['departure_time']}\n"
                f"👥 {b['passenger_count']} passengers | 💰 MVR {b['total_amount']}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{b['id']}")
                ]]))

# ── HELPERS ───────────────────────────────────────────────────────────────────
async def save_operator(user, temp: dict):
    import json as _json
    # Build payment accounts list
    accounts = []
    if temp.get("bml_account"):
        parts = temp["bml_account"].split("|", 1)
        accounts.append({"bank": "BML", "number": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    if temp.get("mib_account"):
        parts = temp["mib_account"].split("|", 1)
        accounts.append({"bank": "MIB", "number": parts[0].strip(), "name": parts[1].strip() if len(parts) > 1 else ""})
    payment_accounts_json = _json.dumps(accounts)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Add payment_accounts column if it doesn't exist
        await conn.execute("""
            ALTER TABLE operators ADD COLUMN IF NOT EXISTS payment_accounts TEXT DEFAULT '[]'
        """)
        await conn.execute("""
            INSERT INTO operators (telegram_id, telegram_username, business_name, boat_name,
                                   logo_url, seat_count, boat_type, routes, owner_name,
                                   owner_contact, owner_id_photo_url, bml_account,
                                   payment_accounts, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'pending')
            ON CONFLICT (telegram_id) DO UPDATE SET
                business_name=EXCLUDED.business_name,
                boat_name=EXCLUDED.boat_name,
                logo_url=EXCLUDED.logo_url,
                owner_name=EXCLUDED.owner_name,
                owner_contact=EXCLUDED.owner_contact,
                owner_id_photo_url=EXCLUDED.owner_id_photo_url,
                bml_account=EXCLUDED.bml_account,
                payment_accounts=EXCLUDED.payment_accounts,
                status='pending'
        """, user.id, user.username, temp.get("business_name"), temp.get("boat_name"),
            temp.get("logo_url"), int(temp.get("seat_count") or 0), temp.get("boat_type"),
            temp.get("routes",[]), temp.get("owner_name"), temp.get("owner_contact"),
            temp.get("owner_id_photo_url"), temp.get("bml_account",""),
            payment_accounts_json)

async def notify_admin_new_op(ctx, user, temp: dict):
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM operators WHERE telegram_id=$1", user.id)
    op_id = row["id"] if row else 0

    msg = (
        f"🆕 *New Operator Application*\n\n"
        f"👤 @{user.username or user.first_name} (`{user.id}`)\n"
        f"🏢 *{temp.get('business_name')}*\n"
        f"🛥️ {temp.get('boat_name')} — {temp.get('seat_count')} seats\n"
        f"📍 {temp.get('boat_type','ferry').title()}\n"
        f"🗺️ {', '.join(temp.get('routes',[]))}\n"
        f"👤 {temp.get('owner_name')} | 📞 {temp.get('owner_contact')}\n"
        f"🏦 BML: `{temp.get('bml_account','N/A')}`\n"
        f"🏦 MIB: `{temp.get('mib_account','—')}`"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_op_{op_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject_op_{op_id}")
    ]])
    try:
        logger.info(f"Sending admin notification to group {ADMIN_GROUP_ID} thread {ADMIN_THREAD_ID}")
        await ctx.bot.send_message(ADMIN_GROUP_ID, msg, parse_mode="Markdown",
                                   message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
        logger.info("✅ Admin notification sent")
    except Exception as e:
        logger.error(f"❌ Admin notify FAILED: {e}")
        # Try without thread ID as fallback
        try:
            await ctx.bot.send_message(ADMIN_GROUP_ID, msg, parse_mode="Markdown", reply_markup=kb)
            logger.info("✅ Admin notification sent (no thread)")
        except Exception as e2:
            logger.error(f"❌ Admin notify fallback FAILED: {e2}")
    try:
        if temp.get("logo_url"):
            await ctx.bot.send_photo(ADMIN_GROUP_ID, photo=temp["logo_url"],
                                     caption="🖼️ Operator Logo", message_thread_id=ADMIN_THREAD_ID)
        if temp.get("owner_id_photo_url"):
            await ctx.bot.send_photo(ADMIN_GROUP_ID, photo=temp["owner_id_photo_url"],
                                     caption="🪪 Owner ID", message_thread_id=ADMIN_THREAD_ID)
    except Exception as e:
        logger.error(f"❌ Admin photo send FAILED: {e}")

async def notify_operator_payment(ctx, booking_id, sel, temp, ref, customer, slip_file_id):
    op_tg_id = sel.get("op_telegram_id") or sel.get("operator_telegram_id")
    if not op_tg_id:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT telegram_id FROM operators WHERE id=$1", sel.get("operator_id"))
        if not row:
            return
        op_tg_id = row["telegram_id"]

    pax = temp.get("passengers_collected",[])
    pax_lines = "\n".join([f"  {i+1}. {p['name']} ({p['id_number']})" for i,p in enumerate(pax)])
    msg = (
        f"💳 *New Payment Received!*\n\n"
        f"🔖 Ref: `{ref}`\n"
        f"👤 *Customer:* {temp.get('cx_name','N/A')} | 📞 {temp.get('cx_phone','N/A')}\n"
        f"📍 {temp.get('route_from')} → {temp.get('route_to')}\n"
        f"📅 {temp.get('travel_date')} @ {sel.get('departure_time')}\n"
        f"👥 {temp.get('passenger_count')} passengers:\n{pax_lines}\n"
        f"💰 MVR {temp.get('total_amount')}\n\n"
        f"Review the slip and confirm below 👇"
    )
    try:
        await ctx.bot.send_photo(op_tg_id, photo=slip_file_id, caption=msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{booking_id}")
            ]]))
    except Exception as e:
        logger.error(f"Operator notify error: {e}")

async def do_confirm_booking(ctx, booking_id: int, query):
    pool = await get_pool()
    async with pool.acquire() as conn:
        booking = await conn.fetchrow("""
            SELECT b.*, o.business_name, o.boat_name, o.logo_url,
                   s.route_from, s.route_to, s.departure_time, s.price_per_seat
            FROM bookings b
            JOIN operators o ON b.operator_id=o.id
            JOIN schedules s ON b.schedule_id=s.id
            WHERE b.id=$1
        """, booking_id)
        if not booking:
            await query.message.reply_text("⚠️ Booking not found.")
            return
        await conn.execute(
            "UPDATE bookings SET status='confirmed', confirmed_at=NOW() WHERE id=$1", booking_id)
        await conn.execute(
            "UPDATE schedules SET available_seats=available_seats-$1 WHERE id=$2",
            booking["passenger_count"], booking["schedule_id"])

    booking_dict = dict(booking)
    passengers = booking_dict.get("passengers", "[]")
    if isinstance(passengers, str):
        booking_dict["passengers"] = json.loads(passengers)

    op_dict    = {"business_name": booking["business_name"], "boat_name": booking["boat_name"], "logo_url": booking["logo_url"]}
    sched_dict = {"route_from": booking["route_from"], "route_to": booking["route_to"],
                  "departure_time": booking["departure_time"], "price_per_seat": booking["price_per_seat"]}

    pdf_bytes = generate_ticket_pdf(booking_dict, op_dict, sched_dict)
    pdf_file  = io.BytesIO(pdf_bytes)
    pdf_file.name = f"ticket_{booking['booking_ref']}.pdf"

    await ctx.bot.send_document(
        booking["customer_telegram_id"], document=pdf_file,
        caption=(
            f"✅ *Booking Confirmed!*\n\n"
            f"🎫 Your ticket is attached.\n"
            f"🔖 Ref: `{booking['booking_ref']}`\n"
            f"🚤 {booking['business_name']}\n"
            f"📍 {booking['route_from']} → {booking['route_to']}\n"
            f"📅 {booking['travel_date']} @ {booking['departure_time']}\n\n"
            f"Present this ticket when boarding. Safe travels! 🌊"
        ), parse_mode="Markdown")

    await query.edit_message_caption(
        caption=f"✅ Booking `{booking['booking_ref']}` confirmed! Ticket sent.", parse_mode="Markdown")

# ── ERROR HANDLER ─────────────────────────────────────────────────────────────
async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception while handling update: {ctx.error}", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Something went wrong. Please try again or send /start")
        except Exception:
            pass

# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    # Init DB first before anything else
    logger.info("🌊 Starting Samuga Travels Bot v1.1...")
    await init_db()
    logger.info("✅ DB ready — building bot...")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("cancel",    cmd_cancel))
    app.add_handler(CommandHandler("register",  cmd_register))
    app.add_handler(CommandHandler("recommend", cmd_recommend))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("🌊 Samuga Travels Bot v1.1 LIVE!")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())

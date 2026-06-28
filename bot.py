"""
Samuga Travels Bot v1.0
A multi-tenant speedboat booking platform for the Maldives.
Single-file architecture. Deployed on Railway + PostgreSQL.
"""

import os
import io
import logging
import asyncio
import hashlib
import cloudinary
import cloudinary.uploader
import requests
from datetime import datetime, timedelta
from decimal import Decimal
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
import psycopg2
from psycopg2.extras import RealDictCursor

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8973602844:AAGXMdvXqNPnTBWZGJNtJLb5ZKcMvjBgGE")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
ADMIN_GROUP_ID = int(os.environ.get("ADMIN_GROUP_ID", "-1004397030483"))
ADMIN_THREAD_ID = int(os.environ.get("ADMIN_THREAD_ID", "2"))
GENERAL_THREAD_ID = int(os.environ.get("GENERAL_THREAD_ID", "1"))

CLOUDINARY_CLOUD = os.environ.get("CLOUDINARY_CLOUD", "dfhj3clbh")
CLOUDINARY_KEY = os.environ.get("CLOUDINARY_KEY", "324844414354471")
CLOUDINARY_SECRET = os.environ.get("CLOUDINARY_SECRET", "F4cmCOwLzIcSyXBhKZFzQDHevOk")

cloudinary.config(cloud_name=CLOUDINARY_CLOUD, api_key=CLOUDINARY_KEY, api_secret=CLOUDINARY_SECRET)

VERSION = "1.0"

# ─── USER STATES ──────────────────────────────────────────────────────────────
# Operator registration states
OP_IDLE = "op_idle"
OP_AWAIT_BUSINESS_NAME = "op_await_business_name"
OP_AWAIT_LOGO = "op_await_logo"
OP_AWAIT_BOAT_NAME = "op_await_boat_name"
OP_AWAIT_SEATS = "op_await_seats"
OP_AWAIT_TYPE = "op_await_type"
OP_AWAIT_ROUTES = "op_await_routes"
OP_AWAIT_OWNER_NAME = "op_await_owner_name"
OP_AWAIT_OWNER_CONTACT = "op_await_owner_contact"
OP_AWAIT_OWNER_ID_PHOTO = "op_await_owner_id_photo"
OP_AWAIT_BML_ACCOUNT = "op_await_bml_account"
OP_REGISTERED = "op_registered"

# Schedule states
OP_AWAIT_SCHEDULE_ROUTE = "op_await_schedule_route"
OP_AWAIT_SCHEDULE_TIME = "op_await_schedule_time"
OP_AWAIT_SCHEDULE_PRICE = "op_await_schedule_price"
OP_AWAIT_SCHEDULE_SEATS = "op_await_schedule_seats"

# Customer booking states
CX_IDLE = "cx_idle"
CX_AWAIT_DATE = "cx_await_date"
CX_AWAIT_BOAT_SELECT = "cx_await_boat_select"
CX_AWAIT_PASSENGER_COUNT = "cx_await_passenger_count"
CX_COLLECTING_PASSENGERS = "cx_collecting_passengers"
CX_AWAIT_PAYMENT_SLIP = "cx_await_payment_slip"
CX_BOOKING_COMPLETE = "cx_booking_complete"

# ─── DATABASE ─────────────────────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
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
            status TEXT DEFAULT 'pending',
            is_recommended BOOLEAN DEFAULT FALSE,
            review_text TEXT,
            average_rating DECIMAL(3,2) DEFAULT 0,
            total_reviews INTEGER DEFAULT 0,
            state TEXT DEFAULT 'op_idle',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
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
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id SERIAL PRIMARY KEY,
            booking_ref TEXT UNIQUE NOT NULL,
            customer_telegram_id BIGINT NOT NULL,
            customer_name TEXT,
            operator_id INTEGER REFERENCES operators(id),
            schedule_id INTEGER REFERENCES schedules(id),
            travel_date DATE NOT NULL,
            passenger_count INTEGER NOT NULL,
            passengers JSONB DEFAULT '[]',
            total_amount DECIMAL(10,2) NOT NULL,
            status TEXT DEFAULT 'pending_payment',
            payment_slip_url TEXT,
            ticket_url TEXT,
            created_at TIMESTAMP DEFAULT NOW(),
            confirmed_at TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_states (
            telegram_id BIGINT PRIMARY KEY,
            role TEXT DEFAULT 'customer',
            state TEXT DEFAULT 'cx_idle',
            temp_data JSONB DEFAULT '{}',
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    cur.execute("""
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

    conn.commit()
    cur.close()
    conn.close()
    logger.info("✅ Database initialized")

# ─── DB HELPERS ───────────────────────────────────────────────────────────────
def get_user_state(telegram_id: int) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM user_states WHERE telegram_id = %s", (telegram_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return dict(row)
    return {"telegram_id": telegram_id, "role": "customer", "state": CX_IDLE, "temp_data": {}}

def set_user_state(telegram_id: int, state: str, temp_data: dict = None, role: str = None):
    conn = get_db()
    cur = conn.cursor()
    if temp_data is None:
        cur.execute("""
            INSERT INTO user_states (telegram_id, state) VALUES (%s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET state = %s, updated_at = NOW()
        """, (telegram_id, state, state))
    elif role:
        import json
        cur.execute("""
            INSERT INTO user_states (telegram_id, state, temp_data, role) VALUES (%s, %s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET state = %s, temp_data = %s, role = %s, updated_at = NOW()
        """, (telegram_id, state, json.dumps(temp_data), role, state, json.dumps(temp_data), role))
    else:
        import json
        cur.execute("""
            INSERT INTO user_states (telegram_id, state, temp_data) VALUES (%s, %s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET state = %s, temp_data = %s, updated_at = NOW()
        """, (telegram_id, state, json.dumps(temp_data), state, json.dumps(temp_data)))
    conn.commit()
    cur.close()
    conn.close()

def update_temp_data(telegram_id: int, key: str, value):
    import json
    state_data = get_user_state(telegram_id)
    temp = state_data.get("temp_data", {}) or {}
    temp[key] = value
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE user_states SET temp_data = %s, updated_at = NOW() WHERE telegram_id = %s
    """, (json.dumps(temp), telegram_id))
    conn.commit()
    cur.close()
    conn.close()

def get_operator(telegram_id: int) -> dict | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM operators WHERE telegram_id = %s", (telegram_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return dict(row) if row else None

def generate_booking_ref() -> str:
    import random, string
    ts = datetime.now().strftime("%y%m%d")
    rand = ''.join(random.choices(string.digits, k=4))
    return f"ST-{ts}-{rand}"

# ─── CLOUDINARY UPLOAD ────────────────────────────────────────────────────────
async def upload_to_cloudinary(file_bytes: bytes, folder: str, filename: str) -> str:
    result = cloudinary.uploader.upload(
        file_bytes,
        folder=f"samuga_travels/{folder}",
        public_id=filename,
        overwrite=True,
        resource_type="image"
    )
    return result["secure_url"]

# ─── PDF TICKET GENERATOR ─────────────────────────────────────────────────────
def generate_ticket_pdf(booking: dict, operator: dict, schedule: dict) -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    story = []

    # Header style
    title_style = ParagraphStyle('title', parent=styles['Title'],
                                  fontSize=22, textColor=colors.HexColor('#1a3a5c'),
                                  spaceAfter=4, alignment=TA_CENTER)
    sub_style = ParagraphStyle('sub', parent=styles['Normal'],
                                fontSize=10, textColor=colors.HexColor('#555555'),
                                alignment=TA_CENTER, spaceAfter=2)
    label_style = ParagraphStyle('label', parent=styles['Normal'],
                                  fontSize=9, textColor=colors.HexColor('#888888'))
    value_style = ParagraphStyle('value', parent=styles['Normal'],
                                  fontSize=11, textColor=colors.HexColor('#1a1a1a'),
                                  fontName='Helvetica-Bold')

    # Operator logo if available
    if operator.get("logo_url"):
        try:
            resp = requests.get(operator["logo_url"], timeout=5)
            img_data = io.BytesIO(resp.content)
            logo = RLImage(img_data, width=50*mm, height=50*mm)
            logo.hAlign = 'CENTER'
            story.append(logo)
            story.append(Spacer(1, 4*mm))
        except:
            pass

    story.append(Paragraph(operator.get("business_name", "Speedboat Service"), title_style))
    story.append(Paragraph("Powered by Samuga Travels", sub_style))
    story.append(Spacer(1, 6*mm))

    # Ticket box
    ticket_data = [
        ["🎫 BOOKING REFERENCE", booking["booking_ref"]],
        ["🚤 BOAT", operator.get("boat_name", "N/A")],
        ["📍 ROUTE", f"{schedule['route_from']} → {schedule['route_to']}"],
        ["📅 DATE", str(booking["travel_date"])],
        ["⏰ DEPARTURE", schedule["departure_time"]],
        ["👥 PASSENGERS", str(booking["passenger_count"])],
        ["💰 TOTAL PAID", f"MVR {booking['total_amount']}"],
    ]

    table = Table(ticket_data, colWidths=[70*mm, 90*mm])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1a3a5c')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTNAME', (1, 0), (1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f0f4f8'), colors.white]),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#ccddee')),
        ('PADDING', (0, 0), (-1, -1), 8),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(table)
    story.append(Spacer(1, 6*mm))

    # Passengers list
    if booking.get("passengers"):
        story.append(Paragraph("Passenger Details", ParagraphStyle('ph', parent=styles['Heading2'],
                                fontSize=12, textColor=colors.HexColor('#1a3a5c'))))
        pax_data = [["#", "Full Name", "ID / Passport"]]
        for i, p in enumerate(booking["passengers"], 1):
            pax_data.append([str(i), p.get("name", ""), p.get("id_number", "")])
        pax_table = Table(pax_data, colWidths=[10*mm, 80*mm, 70*mm])
        pax_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2e86ab')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f7fbff'), colors.white]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#c0d8f0')),
            ('PADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(pax_table)
        story.append(Spacer(1, 6*mm))

    # Footer
    story.append(Paragraph("✅ Present this ticket when boarding.",
                            ParagraphStyle('footer', parent=styles['Normal'],
                                           fontSize=9, textColor=colors.HexColor('#444444'),
                                           alignment=TA_CENTER)))
    story.append(Paragraph("Samuga Travels — Safe travels! 🌊",
                            ParagraphStyle('brand', parent=styles['Normal'],
                                           fontSize=8, textColor=colors.HexColor('#888888'),
                                           alignment=TA_CENTER)))

    doc.build(story)
    return buffer.getvalue()

# ─── KEYBOARDS ────────────────────────────────────────────────────────────────
def main_menu_keyboard(role: str = "customer"):
    if role == "operator":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Profile", callback_data="op_profile"),
             InlineKeyboardButton("🗓️ Manage Schedules", callback_data="op_schedules")],
            [InlineKeyboardButton("📦 View Bookings", callback_data="op_bookings"),
             InlineKeyboardButton("✏️ Edit Info", callback_data="op_edit")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Search Boats", callback_data="cx_search"),
         InlineKeyboardButton("📋 My Bookings", callback_data="cx_my_bookings")],
        [InlineKeyboardButton("🤝 Register as Operator", callback_data="register_operator")],
    ])

def boat_type_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⛴️ Ferry Service", callback_data="type_ferry")],
        [InlineKeyboardButton("🛥️ Private Hire", callback_data="type_private")],
    ])

# ─── COMMAND HANDLERS ─────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state_data = get_user_state(user.id)
    role = state_data.get("role", "customer")

    welcome = (
        f"🌊 *Welcome to Samuga Travels!*\n\n"
        f"The Maldives' smartest speedboat booking platform.\n\n"
        f"Hi *{user.first_name}*! What would you like to do?"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown",
                                     reply_markup=main_menu_keyboard(role))

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🌊 *Samuga Travels Help*\n\n"
        "*For Customers:*\n"
        "• Search boats by route\n"
        "• Book seats & pay via BML transfer\n"
        "• Get auto-generated PDF ticket\n\n"
        "*For Operators:*\n"
        "• /register — Register your speedboat\n"
        "• /schedules — Manage your routes\n"
        "• /bookings — View your bookings\n\n"
        "*Commands:*\n"
        "/start — Main menu\n"
        "/register — Register as operator\n"
        "/cancel — Cancel current action"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state_data = get_user_state(user.id)
    role = state_data.get("role", "customer")
    set_user_state(user.id, CX_IDLE if role == "customer" else OP_IDLE, {})
    await update.message.reply_text("❌ Action cancelled. Back to main menu.",
                                     reply_markup=main_menu_keyboard(role))

# ─── OPERATOR REGISTRATION ────────────────────────────────────────────────────
async def start_operator_registration(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        user = query.from_user
        msg = query.message
    else:
        user = update.effective_user
        msg = update.message

    existing = get_operator(user.id)
    if existing:
        status = existing.get("status", "pending")
        if status == "approved":
            set_user_state(user.id, OP_IDLE, {}, role="operator")
            await msg.reply_text("✅ You're already a verified operator! Use /start to manage your account.")
        elif status == "pending":
            await msg.reply_text("⏳ Your application is under review. We'll notify you once approved.")
        elif status == "rejected":
            await msg.reply_text("❌ Your previous application was rejected. Contact @SamugaTravels for support.")
        return

    set_user_state(user.id, OP_AWAIT_BUSINESS_NAME, {}, role="operator_pending")
    text = (
        "🚤 *Operator Registration — Samuga Travels*\n\n"
        "Let's get your speedboat listed!\n\n"
        "*Step 1 of 9:* What is your business/company name?\n\n"
        "_Example: Thoddoo Express Travels_"
    )
    await msg.reply_text(text, parse_mode="Markdown")

# ─── MESSAGE HANDLER (State Machine) ──────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state_data = get_user_state(user.id)
    state = state_data.get("state", CX_IDLE)
    temp = state_data.get("temp_data", {}) or {}
    text = update.message.text or ""

    # ── OPERATOR REGISTRATION FLOW ──
    if state == OP_AWAIT_BUSINESS_NAME:
        update_temp_data(user.id, "business_name", text.strip())
        set_user_state(user.id, OP_AWAIT_BOAT_NAME, {**temp, "business_name": text.strip()})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 2 of 9:* What is your *boat name*?\n\n_Example: Ocean Star_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BOAT_NAME:
        update_temp_data(user.id, "boat_name", text.strip())
        set_user_state(user.id, OP_AWAIT_SEATS, {**temp, "boat_name": text.strip()})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 3 of 9:* How many *seats* does your boat have?\n\n_Enter a number, e.g. 20_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SEATS:
        if not text.strip().isdigit():
            await update.message.reply_text("⚠️ Please enter a valid number of seats.")
            return
        update_temp_data(user.id, "seat_count", int(text.strip()))
        set_user_state(user.id, OP_AWAIT_TYPE, {**temp, "seat_count": int(text.strip())})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 4 of 9:* What type of service do you offer?",
            parse_mode="Markdown",
            reply_markup=boat_type_keyboard())

    elif state == OP_AWAIT_ROUTES:
        routes = [r.strip() for r in text.split(",") if r.strip()]
        if len(routes) < 1:
            await update.message.reply_text("⚠️ Please enter at least one route.")
            return
        update_temp_data(user.id, "routes", routes)
        set_user_state(user.id, OP_AWAIT_OWNER_NAME, {**temp, "routes": routes})
        await update.message.reply_text(
            "✅ Routes saved!\n\n*Step 6 of 9:* What is the *owner's full name*?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_NAME:
        update_temp_data(user.id, "owner_name", text.strip())
        set_user_state(user.id, OP_AWAIT_OWNER_CONTACT, {**temp, "owner_name": text.strip()})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 7 of 9:* What is the *owner's contact number*?\n\n_Example: 7771234_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_CONTACT:
        update_temp_data(user.id, "owner_contact", text.strip())
        set_user_state(user.id, OP_AWAIT_OWNER_ID_PHOTO, {**temp, "owner_contact": text.strip()})
        await update.message.reply_text(
            "✅ Got it!\n\n*Step 8 of 9:* Please upload a *photo of the owner's ID card or passport*.\n\n"
            "_This is for verification purposes only and will be kept secure._",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BML_ACCOUNT:
        update_temp_data(user.id, "bml_account", text.strip())
        set_user_state(user.id, OP_REGISTERED, {**temp, "bml_account": text.strip()})
        # Save operator to DB
        await save_operator_to_db(user, {**temp, "bml_account": text.strip()})
        await notify_admin_new_operator(context, user, {**temp, "bml_account": text.strip()})
        await update.message.reply_text(
            "🎉 *Registration Complete!*\n\n"
            "Your application has been submitted to the Samuga Travels team for review.\n\n"
            "⏳ *What happens next?*\n"
            "• Our team will review your application\n"
            "• We'll verify your ID and boat details\n"
            "• You'll be notified here once approved\n\n"
            "Approval usually takes within 24 hours. Thank you! 🌊",
            parse_mode="Markdown")

    # ── SCHEDULE MANAGEMENT ──
    elif state == OP_AWAIT_SCHEDULE_ROUTE:
        parts = [p.strip() for p in text.split("to")]
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Format: `Male to Thoddoo`", parse_mode="Markdown")
            return
        update_temp_data(user.id, "sched_from", parts[0])
        update_temp_data(user.id, "sched_to", parts[1])
        set_user_state(user.id, OP_AWAIT_SCHEDULE_TIME, {**temp, "sched_from": parts[0], "sched_to": parts[1]})
        await update.message.reply_text(
            "✅ Route saved!\n\nWhat is the *departure time*?\n\n_Example: 04:00 PM_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_TIME:
        update_temp_data(user.id, "sched_time", text.strip())
        set_user_state(user.id, OP_AWAIT_SCHEDULE_PRICE, {**temp, "sched_time": text.strip()})
        await update.message.reply_text(
            "✅ Time saved!\n\nWhat is the *price per seat* (MVR)?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_PRICE:
        try:
            price = float(text.strip())
        except ValueError:
            await update.message.reply_text("⚠️ Please enter a valid price. Example: `535`", parse_mode="Markdown")
            return
        update_temp_data(user.id, "sched_price", price)
        set_user_state(user.id, OP_AWAIT_SCHEDULE_SEATS, {**temp, "sched_price": price})
        await update.message.reply_text(
            "✅ Price saved!\n\nHow many *available seats* for this schedule?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_SEATS:
        if not text.strip().isdigit():
            await update.message.reply_text("⚠️ Please enter a valid number.")
            return
        seats = int(text.strip())
        state_data2 = get_user_state(user.id)
        temp2 = state_data2.get("temp_data", {}) or {}
        operator = get_operator(user.id)
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO schedules (operator_id, route_from, route_to, departure_time, price_per_seat, total_seats, available_seats)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (operator["id"], temp2.get("sched_from"), temp2.get("sched_to"),
              temp2.get("sched_time"), temp2.get("sched_price"), seats, seats))
        conn.commit()
        cur.close()
        conn.close()
        set_user_state(user.id, OP_IDLE, {})
        await update.message.reply_text(
            f"✅ *Schedule Added!*\n\n"
            f"📍 {temp2.get('sched_from')} → {temp2.get('sched_to')}\n"
            f"⏰ {temp2.get('sched_time')}\n"
            f"💰 MVR {temp2.get('sched_price')}/seat\n"
            f"👥 {seats} seats",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard("operator"))

    # ── CUSTOMER BOOKING FLOW ──
    elif state == CX_AWAIT_DATE:
        try:
            travel_date = datetime.strptime(text.strip(), "%d-%m-%Y").date()
        except ValueError:
            await update.message.reply_text("⚠️ Invalid date format. Please use DD-MM-YYYY\n\nExample: `30-06-2026`",
                                             parse_mode="Markdown")
            return
        if travel_date < datetime.now().date():
            await update.message.reply_text("⚠️ Travel date cannot be in the past.")
            return

        route_from = temp.get("route_from", "")
        route_to = temp.get("route_to", "")

        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT s.*, o.business_name, o.boat_name, o.logo_url, o.is_recommended,
                   o.average_rating, o.total_reviews, o.review_text, o.bml_account
            FROM schedules s
            JOIN operators o ON s.operator_id = o.id
            WHERE LOWER(s.route_from) LIKE %s AND LOWER(s.route_to) LIKE %s
              AND o.status = 'approved' AND s.is_active = TRUE AND s.available_seats > 0
            ORDER BY o.is_recommended DESC, s.departure_time ASC
        """, (f"%{route_from.lower()}%", f"%{route_to.lower()}%"))
        schedules = cur.fetchall()
        cur.close()
        conn.close()

        if not schedules:
            await update.message.reply_text(
                f"😔 No available boats found for *{route_from} → {route_to}* on *{text.strip()}*.\n\n"
                f"Try a different date or route.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔍 Search Again", callback_data="cx_search")
                ]]))
            set_user_state(user.id, CX_IDLE, {})
            return

        update_temp_data(user.id, "travel_date", str(travel_date))
        update_temp_data(user.id, "schedules", [dict(s) for s in schedules])

        msg_text = f"🚢 *Available Boats — {route_from} → {route_to}*\n📅 *{text.strip()}*\n\n"
        buttons = []

        for i, s in enumerate(schedules):
            stars = "⭐" * int(s["average_rating"]) if s["average_rating"] else "No ratings yet"
            recommended = "✨ *Recommended by Samuga Travels*\n" if s["is_recommended"] else ""
            msg_text += (
                f"{'─' * 30}\n"
                f"🚤 *{s['business_name']}* — _{s['boat_name']}_\n"
                f"{recommended}"
                f"⏰ Departure: *{s['departure_time']}*\n"
                f"💺 Seats available: *{s['available_seats']}*\n"
                f"💰 Price: *MVR {s['price_per_seat']}/seat*\n"
                f"⭐ Rating: {stars} ({s['total_reviews']} reviews)\n"
            )
            if s.get("review_text"):
                msg_text += f"💬 _{s['review_text']}_\n"
            msg_text += "\n"
            buttons.append([InlineKeyboardButton(
                f"Book — {s['business_name']} ({s['departure_time']})",
                callback_data=f"book_schedule_{i}"
            )])

        await update.message.reply_text(msg_text, parse_mode="Markdown",
                                         reply_markup=InlineKeyboardMarkup(buttons))

    elif state == CX_AWAIT_PASSENGER_COUNT:
        if not text.strip().isdigit() or int(text.strip()) < 1:
            await update.message.reply_text("⚠️ Please enter a valid number (1-10).")
            return
        count = int(text.strip())
        if count > 10:
            await update.message.reply_text("⚠️ Maximum 10 seats per booking.")
            return

        schedules = temp.get("schedules", [])
        sched_idx = temp.get("selected_schedule_idx", 0)
        selected = schedules[sched_idx] if schedules else {}

        if count > selected.get("available_seats", 0):
            await update.message.reply_text(
                f"⚠️ Only *{selected.get('available_seats')} seats* available.",
                parse_mode="Markdown")
            return

        update_temp_data(user.id, "passenger_count", count)
        update_temp_data(user.id, "passengers_collected", [])
        update_temp_data(user.id, "current_passenger", 1)
        set_user_state(user.id, CX_COLLECTING_PASSENGERS,
                       {**temp, "passenger_count": count, "passengers_collected": [], "current_passenger": 1})
        await update.message.reply_text(
            f"👤 *Passenger 1 of {count}*\n\n"
            f"Please enter *Full Name* and *ID / Passport Number*:\n\n"
            f"_Format: Ahmed Ali, A123456_",
            parse_mode="Markdown")

    elif state == CX_COLLECTING_PASSENGERS:
        parts = text.split(",", 1)
        if len(parts) != 2:
            await update.message.reply_text("⚠️ Format: `Full Name, ID Number`\n\nExample: `Ahmed Ali, A123456`",
                                             parse_mode="Markdown")
            return

        state_data2 = get_user_state(user.id)
        temp2 = state_data2.get("temp_data", {}) or {}
        passengers = temp2.get("passengers_collected", [])
        current = temp2.get("current_passenger", 1)
        total = temp2.get("passenger_count", 1)

        passengers.append({"name": parts[0].strip(), "id_number": parts[1].strip()})
        update_temp_data(user.id, "passengers_collected", passengers)

        if current < total:
            update_temp_data(user.id, "current_passenger", current + 1)
            set_user_state(user.id, CX_COLLECTING_PASSENGERS,
                           {**temp2, "passengers_collected": passengers, "current_passenger": current + 1})
            await update.message.reply_text(
                f"✅ Passenger {current} saved!\n\n"
                f"👤 *Passenger {current + 1} of {total}*\n\n"
                f"Please enter *Full Name* and *ID / Passport Number*:\n\n"
                f"_Format: Ahmed Ali, A123456_",
                parse_mode="Markdown")
        else:
            # All passengers collected — show summary
            state_data3 = get_user_state(user.id)
            temp3 = state_data3.get("temp_data", {}) or {}
            schedules = temp3.get("schedules", [])
            sched_idx = temp3.get("selected_schedule_idx", 0)
            selected = schedules[sched_idx] if schedules else {}
            total_amount = Decimal(str(selected.get("price_per_seat", 0))) * total

            pax_list = "\n".join([f"  {i+1}. {p['name']} ({p['id_number']})"
                                   for i, p in enumerate(passengers)])
            summary = (
                f"📝 *Booking Summary*\n\n"
                f"🚤 *Operator:* {selected.get('business_name')}\n"
                f"🛥️ *Boat:* {selected.get('boat_name')}\n"
                f"📍 *Route:* {temp3.get('route_from')} → {temp3.get('route_to')}\n"
                f"📅 *Date:* {temp3.get('travel_date')}\n"
                f"⏰ *Departure:* {selected.get('departure_time')}\n"
                f"👥 *Passengers ({total}):*\n{pax_list}\n\n"
                f"💰 *Total Amount:* MVR {total_amount}\n\n"
                f"{'─' * 30}\n"
                f"💳 *Payment Instructions:*\n"
                f"Transfer *MVR {total_amount}* to:\n\n"
                f"🏦 *BML Account:* `{selected.get('bml_account', 'N/A')}`\n"
                f"📛 *Account Name:* {selected.get('business_name')}\n\n"
                f"👉 *After transfer, please upload your BML payment screenshot here.*"
            )
            update_temp_data(user.id, "total_amount", str(total_amount))
            update_temp_data(user.id, "passengers_collected", passengers)
            set_user_state(user.id, CX_AWAIT_PAYMENT_SLIP,
                           {**temp3, "total_amount": str(total_amount), "passengers_collected": passengers})
            await update.message.reply_text(summary, parse_mode="Markdown")

    else:
        # Default — check if they typed a route
        if "to" in text.lower():
            parts = text.lower().split("to", 1)
            route_from = parts[0].strip().title()
            route_to = parts[1].strip().title()
            set_user_state(user.id, CX_AWAIT_DATE,
                           {"route_from": route_from, "route_to": route_to})
            await update.message.reply_text(
                f"🔍 Searching for boats: *{route_from} → {route_to}*\n\n"
                f"📅 What is your *travel date*?\n\n"
                f"_Format: DD-MM-YYYY (Example: 30-06-2026)_",
                parse_mode="Markdown")
        else:
            state_data = get_user_state(user.id)
            role = state_data.get("role", "customer")
            await update.message.reply_text(
                "👋 Type a route like *Male to Thoddoo* to search boats, or use the menu below.",
                parse_mode="Markdown",
                reply_markup=main_menu_keyboard(role))

# ─── PHOTO HANDLER ────────────────────────────────────────────────────────────
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    state_data = get_user_state(user.id)
    state = state_data.get("state", CX_IDLE)
    temp = state_data.get("temp_data", {}) or {}

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    file_bytes = await file.download_as_bytearray()

    if state == OP_AWAIT_LOGO:
        await update.message.reply_text("⏳ Uploading your logo...")
        url = await upload_to_cloudinary(bytes(file_bytes), "logos", f"logo_{user.id}")
        update_temp_data(user.id, "logo_url", url)
        set_user_state(user.id, OP_AWAIT_ROUTES, {**temp, "logo_url": url})
        await update.message.reply_text(
            "✅ Logo uploaded!\n\n*Step 5 of 9:* What *routes* does your boat cover?\n\n"
            "_Enter routes separated by commas_\n"
            "_Example: Male to Thoddoo, Male to Dhidhdhoo_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_OWNER_ID_PHOTO:
        await update.message.reply_text("⏳ Uploading ID photo securely...")
        url = await upload_to_cloudinary(bytes(file_bytes), "id_photos", f"id_{user.id}")
        update_temp_data(user.id, "owner_id_photo_url", url)
        set_user_state(user.id, OP_AWAIT_BML_ACCOUNT, {**temp, "owner_id_photo_url": url})
        await update.message.reply_text(
            "✅ ID uploaded securely!\n\n"
            "*Step 9 of 9:* What is your *BML bank account number*?\n\n"
            "_This is where customers will transfer payments_\n"
            "_Example: 7730001234567_",
            parse_mode="Markdown")

    elif state == CX_AWAIT_PAYMENT_SLIP:
        await update.message.reply_text("⏳ Processing your payment slip...")
        ref = generate_booking_ref()
        url = await upload_to_cloudinary(bytes(file_bytes), "payment_slips", f"slip_{ref}")

        state_data2 = get_user_state(user.id)
        temp2 = state_data2.get("temp_data", {}) or {}
        schedules = temp2.get("schedules", [])
        sched_idx = temp2.get("selected_schedule_idx", 0)
        selected = schedules[sched_idx] if schedules else {}

        # Save booking to DB
        conn = get_db()
        cur = conn.cursor()
        import json
        cur.execute("""
            INSERT INTO bookings (booking_ref, customer_telegram_id, operator_id, schedule_id,
                                  travel_date, passenger_count, passengers, total_amount, payment_slip_url, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending_confirmation')
            RETURNING id
        """, (ref, user.id, selected.get("operator_id"), selected.get("id"),
              temp2.get("travel_date"), temp2.get("passenger_count"),
              json.dumps(temp2.get("passengers_collected", [])),
              temp2.get("total_amount"), url))
        booking_id = cur.fetchone()["id"]
        conn.commit()
        cur.close()
        conn.close()

        set_user_state(user.id, CX_BOOKING_COMPLETE, {"booking_ref": ref, "booking_id": booking_id})

        await update.message.reply_text(
            "✅ *Payment slip received!*\n\n"
            "Your booking is being reviewed by the operator.\n"
            f"📋 *Booking Ref:* `{ref}`\n\n"
            "You will receive your ticket here once confirmed. Usually within 5 minutes. 🌊",
            parse_mode="Markdown")

        # Forward to operator
        await notify_operator_payment(context, booking_id, selected, temp2, ref, user, photo.file_id)

    else:
        await update.message.reply_text("⚠️ I wasn't expecting an image right now. Use /start to go back to the menu.")

# ─── CALLBACK QUERY HANDLER ───────────────────────────────────────────────────
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    data = query.data
    state_data = get_user_state(user.id)
    temp = state_data.get("temp_data", {}) or {}

    if data == "register_operator":
        await start_operator_registration(update, context)

    elif data == "cx_search":
        set_user_state(user.id, CX_IDLE, {})
        await query.message.reply_text(
            "🔍 *Search for Boats*\n\nType your route like:\n`Male to Thoddoo`\n`Thoddoo to Male`",
            parse_mode="Markdown")

    elif data == "cx_my_bookings":
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT b.*, o.business_name, o.boat_name
            FROM bookings b JOIN operators o ON b.operator_id = o.id
            WHERE b.customer_telegram_id = %s ORDER BY b.created_at DESC LIMIT 5
        """, (user.id,))
        bks = cur.fetchall()
        cur.close()
        conn.close()
        if not bks:
            await query.message.reply_text("📋 You have no bookings yet.")
            return
        msg = "📋 *Your Recent Bookings:*\n\n"
        for b in bks:
            status_icon = {"pending_payment": "⏳", "pending_confirmation": "🔄",
                           "confirmed": "✅", "cancelled": "❌"}.get(b["status"], "❓")
            msg += (f"{status_icon} `{b['booking_ref']}` — {b['business_name']}\n"
                    f"   📅 {b['travel_date']} | MVR {b['total_amount']}\n\n")
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data.startswith("book_schedule_"):
        idx = int(data.split("_")[-1])
        schedules = temp.get("schedules", [])
        if idx >= len(schedules):
            await query.message.reply_text("⚠️ Invalid selection.")
            return
        update_temp_data(user.id, "selected_schedule_idx", idx)
        set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT,
                       {**temp, "selected_schedule_idx": idx})
        selected = schedules[idx]
        await query.message.reply_text(
            f"✅ *{selected['business_name']}* selected!\n\n"
            f"⏰ {selected['departure_time']} | 💺 {selected['available_seats']} seats available\n\n"
            f"How many seats would you like to book? _(Max 10)_",
            parse_mode="Markdown")

    elif data.startswith("type_"):
        boat_type = data.split("_")[1]
        update_temp_data(user.id, "boat_type", boat_type)
        set_user_state(user.id, OP_AWAIT_LOGO, {**temp, "boat_type": boat_type})
        await query.message.reply_text(
            f"✅ Service type: *{'Ferry' if boat_type == 'ferry' else 'Private Hire'}*\n\n"
            f"*Step 5 of 9:* Please upload your *boat/company logo*.",
            parse_mode="Markdown")

    elif data.startswith("approve_op_"):
        op_id = int(data.split("_")[-1])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE operators SET status = 'approved' WHERE id = %s RETURNING telegram_id, business_name",
                    (op_id,))
        op = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if op:
            set_user_state(op["telegram_id"], OP_IDLE, {}, role="operator")
            await context.bot.send_message(op["telegram_id"],
                f"🎉 *Congratulations!*\n\n"
                f"Your operator account for *{op['business_name']}* has been *approved* by Samuga Travels!\n\n"
                f"You can now:\n"
                f"• Add your schedules\n"
                f"• Receive bookings\n"
                f"• Confirm payments\n\n"
                f"Use /start to manage your account. 🌊",
                parse_mode="Markdown")
            await query.edit_message_text(f"✅ Operator *{op['business_name']}* approved!", parse_mode="Markdown")

    elif data.startswith("reject_op_"):
        op_id = int(data.split("_")[-1])
        conn = get_db()
        cur = conn.cursor()
        cur.execute("UPDATE operators SET status = 'rejected' WHERE id = %s RETURNING telegram_id, business_name",
                    (op_id,))
        op = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        if op:
            await context.bot.send_message(op["telegram_id"],
                f"❌ Your application for *{op['business_name']}* was not approved.\n"
                f"Contact @SamugaTravels for more information.",
                parse_mode="Markdown")
            await query.edit_message_text(f"❌ Operator *{op['business_name']}* rejected.", parse_mode="Markdown")

    elif data.startswith("confirm_booking_"):
        booking_id = int(data.split("_")[-1])
        await confirm_booking_and_send_ticket(context, booking_id, query)

    elif data == "op_schedules":
        operator = get_operator(user.id)
        if not operator or operator.get("status") != "approved":
            await query.message.reply_text("⚠️ Your account is not yet approved.")
            return
        set_user_state(user.id, OP_AWAIT_SCHEDULE_ROUTE, {})
        await query.message.reply_text(
            "🗓️ *Add a New Schedule*\n\n"
            "Enter the route:\n_Format: Male to Thoddoo_",
            parse_mode="Markdown")

    elif data == "op_profile":
        operator = get_operator(user.id)
        if not operator:
            await query.message.reply_text("⚠️ No operator profile found.")
            return
        routes = ", ".join(operator.get("routes") or [])
        await query.message.reply_text(
            f"🚤 *Your Operator Profile*\n\n"
            f"🏢 *Business:* {operator['business_name']}\n"
            f"🛥️ *Boat:* {operator['boat_name']}\n"
            f"💺 *Seats:* {operator['seat_count']}\n"
            f"📍 *Routes:* {routes}\n"
            f"📊 *Status:* {operator['status'].upper()}\n"
            f"⭐ *Rating:* {operator['average_rating']} ({operator['total_reviews']} reviews)\n"
            f"✨ *Recommended:* {'Yes' if operator['is_recommended'] else 'No'}\n",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard("operator"))

    elif data == "op_bookings":
        operator = get_operator(user.id)
        if not operator:
            return
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT b.*, s.route_from, s.route_to, s.departure_time
            FROM bookings b JOIN schedules s ON b.schedule_id = s.id
            WHERE b.operator_id = %s AND b.status = 'pending_confirmation'
            ORDER BY b.created_at DESC LIMIT 10
        """, (operator["id"],))
        bks = cur.fetchall()
        cur.close()
        conn.close()
        if not bks:
            await query.message.reply_text("📦 No pending bookings.")
            return
        for b in bks:
            await query.message.reply_text(
                f"📦 *Pending Booking*\n\n"
                f"🔖 Ref: `{b['booking_ref']}`\n"
                f"📍 {b['route_from']} → {b['route_to']}\n"
                f"📅 {b['travel_date']} @ {b['departure_time']}\n"
                f"👥 {b['passenger_count']} passengers\n"
                f"💰 MVR {b['total_amount']}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Confirm Payment", callback_data=f"confirm_booking_{b['id']}")
                ]]))

# ─── HELPERS ──────────────────────────────────────────────────────────────────
async def save_operator_to_db(user, temp: dict):
    conn = get_db()
    cur = conn.cursor()
    import json
    cur.execute("""
        INSERT INTO operators (telegram_id, telegram_username, business_name, boat_name, logo_url,
                               seat_count, boat_type, routes, owner_name, owner_contact,
                               owner_id_photo_url, bml_account, status)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending')
        ON CONFLICT (telegram_id) DO UPDATE SET
            business_name = EXCLUDED.business_name,
            boat_name = EXCLUDED.boat_name,
            logo_url = EXCLUDED.logo_url,
            status = 'pending'
    """, (user.id, user.username, temp.get("business_name"), temp.get("boat_name"),
          temp.get("logo_url"), temp.get("seat_count"), temp.get("boat_type"),
          temp.get("routes", []), temp.get("owner_name"), temp.get("owner_contact"),
          temp.get("owner_id_photo_url"), temp.get("bml_account")))
    conn.commit()
    cur.close()
    conn.close()

async def notify_admin_new_operator(context, user, temp: dict):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id FROM operators WHERE telegram_id = %s", (user.id,))
    op = cur.fetchone()
    cur.close()
    conn.close()
    op_id = op["id"] if op else 0

    msg = (
        f"🆕 *New Operator Application*\n\n"
        f"👤 Telegram: @{user.username or user.first_name} (`{user.id}`)\n"
        f"🏢 Business: *{temp.get('business_name')}*\n"
        f"🛥️ Boat: {temp.get('boat_name')} ({temp.get('seat_count')} seats)\n"
        f"📍 Type: {temp.get('boat_type', 'ferry').title()}\n"
        f"🗺️ Routes: {', '.join(temp.get('routes', []))}\n"
        f"👤 Owner: {temp.get('owner_name')} | 📞 {temp.get('owner_contact')}\n"
        f"🏦 BML: `{temp.get('bml_account')}`\n\n"
        f"🖼️ Logo & ID photo uploaded to Cloudinary."
    )
    buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve_op_{op_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"reject_op_{op_id}")]
    ])
    await context.bot.send_message(ADMIN_GROUP_ID, msg, parse_mode="Markdown",
                                    message_thread_id=ADMIN_THREAD_ID,
                                    reply_markup=buttons)
    if temp.get("logo_url"):
        await context.bot.send_photo(ADMIN_GROUP_ID, photo=temp["logo_url"],
                                      caption="🖼️ Operator Logo",
                                      message_thread_id=ADMIN_THREAD_ID)
    if temp.get("owner_id_photo_url"):
        await context.bot.send_photo(ADMIN_GROUP_ID, photo=temp["owner_id_photo_url"],
                                      caption="🪪 Owner ID Photo",
                                      message_thread_id=ADMIN_THREAD_ID)

async def notify_operator_payment(context, booking_id: int, selected: dict, temp: dict,
                                   ref: str, customer, slip_file_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id FROM operators WHERE id = %s", (selected.get("operator_id"),))
    op = cur.fetchone()
    cur.close()
    conn.close()

    if not op:
        return

    pax_list = "\n".join([f"  {i+1}. {p['name']} ({p['id_number']})"
                           for i, p in enumerate(temp.get("passengers_collected", []))])
    msg = (
        f"💳 *New Payment Received!*\n\n"
        f"🔖 Booking Ref: `{ref}`\n"
        f"📍 Route: {temp.get('route_from')} → {temp.get('route_to')}\n"
        f"📅 Date: {temp.get('travel_date')} @ {selected.get('departure_time')}\n"
        f"👥 Passengers ({temp.get('passenger_count')}):\n{pax_list}\n"
        f"💰 Total: MVR {temp.get('total_amount')}\n\n"
        f"👆 Review the payment slip and confirm below."
    )
    try:
        await context.bot.send_photo(
            op["telegram_id"],
            photo=slip_file_id,
            caption=msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{booking_id}")
            ]]))
    except Exception as e:
        logger.error(f"Could not notify operator: {e}")

async def confirm_booking_and_send_ticket(context, booking_id: int, query):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        SELECT b.*, o.business_name, o.boat_name, o.logo_url,
               s.route_from, s.route_to, s.departure_time, s.price_per_seat
        FROM bookings b
        JOIN operators o ON b.operator_id = o.id
        JOIN schedules s ON b.schedule_id = s.id
        WHERE b.id = %s
    """, (booking_id,))
    booking = cur.fetchone()

    if not booking:
        await query.message.reply_text("⚠️ Booking not found.")
        cur.close()
        conn.close()
        return

    # Update status
    cur.execute("""
        UPDATE bookings SET status = 'confirmed', confirmed_at = NOW() WHERE id = %s
    """, (booking_id,))

    # Reduce available seats
    cur.execute("""
        UPDATE schedules SET available_seats = available_seats - %s WHERE id = %s
    """, (booking["passenger_count"], booking["schedule_id"]))

    conn.commit()
    cur.close()
    conn.close()

    # Generate PDF ticket
    booking_dict = dict(booking)
    operator_dict = {
        "business_name": booking["business_name"],
        "boat_name": booking["boat_name"],
        "logo_url": booking["logo_url"]
    }
    schedule_dict = {
        "route_from": booking["route_from"],
        "route_to": booking["route_to"],
        "departure_time": booking["departure_time"],
        "price_per_seat": booking["price_per_seat"]
    }

    pdf_bytes = generate_ticket_pdf(booking_dict, operator_dict, schedule_dict)
    pdf_file = io.BytesIO(pdf_bytes)
    pdf_file.name = f"ticket_{booking['booking_ref']}.pdf"

    await context.bot.send_document(
        booking["customer_telegram_id"],
        document=pdf_file,
        caption=(
            f"✅ *Booking Confirmed!*\n\n"
            f"🎫 Your ticket is attached.\n"
            f"🔖 Ref: `{booking['booking_ref']}`\n"
            f"🚤 {booking['business_name']}\n"
            f"📍 {booking['route_from']} → {booking['route_to']}\n"
            f"📅 {booking['travel_date']} @ {booking['departure_time']}\n\n"
            f"Present this ticket when boarding. Have a safe trip! 🌊"
        ),
        parse_mode="Markdown"
    )

    await query.edit_message_caption(
        caption=f"✅ *Booking `{booking['booking_ref']}` confirmed!* Ticket sent to customer.",
        parse_mode="Markdown")

# ─── ADMIN COMMANDS ───────────────────────────────────────────────────────────
async def admin_recommend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /recommend <operator_telegram_id> <review_text>"""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /recommend <telegram_id> <review text>")
        return
    op_tid = int(args[0])
    review_text = " ".join(args[1:])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE operators SET is_recommended = TRUE, review_text = %s WHERE telegram_id = %s
        RETURNING business_name
    """, (review_text, op_tid))
    op = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if op:
        await update.message.reply_text(f"✅ *{op['business_name']}* is now Recommended by Samuga Travels!",
                                         parse_mode="Markdown")
        await context.bot.send_message(op_tid,
            f"🌟 *Congratulations!*\n\nYour business has been marked as *Recommended by Samuga Travels!*\n\n"
            f"💬 Review: _{review_text}_\n\nCustomers will see this badge when browsing boats. 🎉",
            parse_mode="Markdown")

# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("register", start_operator_registration))
    app.add_handler(CommandHandler("recommend", admin_recommend))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🌊 Samuga Travels Bot v1.0 starting...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

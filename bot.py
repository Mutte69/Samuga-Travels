"""
Samuga Travels Bot v1.2
Multi-tenant speedboat booking platform for the Maldives.
Single-file | asyncpg | Railway + PostgreSQL | Cloudinary
"""

import os, io, logging, asyncio, json, random, string, signal
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
# Your personal Telegram ID — gets full admin access
SUPER_ADMINS    = [int(x) for x in os.environ.get("SUPER_ADMINS", "").split(",") if x.strip().isdigit()]

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
# Fleet/boat states
OP_AWAIT_BOAT_ADD_NAME="op_await_boat_add_name"
OP_AWAIT_BOAT_ADD_CAPACITY="op_await_boat_add_capacity"
# Schedule extra states
OP_AWAIT_SCHEDULE_LOCATION="op_await_schedule_location"
OP_AWAIT_SCHEDULE_DAYS="op_await_schedule_days"
OP_AWAIT_CHANGE_NOTE="op_await_change_note"
# Admin states
ADMIN_AWAIT_BROADCAST="admin_await_broadcast"
ADMIN_AWAIT_LOGO="admin_await_logo"
ADMIN_AWAIT_REVIEW_TEXT="admin_await_review_text"

# ── SMART INPUT HELPERS ──────────────────────────────────────────────────────
def normalize_input(text: str) -> str:
    """Clean up common input variations"""
    return text.strip()

def parse_name_id(text: str) -> tuple[str, str] | None:
    """
    Flexibly parse 'Name, ID' from user input.
    Accepts: comma, dash, slash, pipe, space+ID as separators.
    Also handles: 'Ahmed Ali A123456' (space before ID starting with A/A0-9)
    """
    import re
    text = text.strip()
    # Try comma first (preferred)
    if "," in text:
        parts = text.split(",", 1)
        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
            return parts[0].strip(), parts[1].strip()
    # Try other separators: | / - (with spaces)
    for sep in [" | ", " / ", " - ", "|", "/"]:
        if sep in text:
            parts = text.split(sep, 1)
            if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                return parts[0].strip(), parts[1].strip()
    # Try: name followed by ID card pattern (A + digits or passport)
    match = re.search(r'^(.+?)\s+([A-Za-z]\d{5,}|[A-Z]{2}\d{6,})$', text)
    if match:
        return match.group(1).strip(), match.group(2).strip()
    return None

def is_cancel(text: str) -> bool:
    """Check if user wants to cancel"""
    return text.strip().lower() in ["cancel", "stop", "quit", "exit", "/cancel", "back", "nope", "no"]

def is_skip(text: str) -> bool:
    """Check if user wants to skip optional step"""
    return text.strip().lower() in ["skip", "no", "nope", "none", "-", "n/a", "na", "next"]

def parse_number(text: str) -> int | None:
    """Extract number from text like '2 seats', '2pax', '2 people'"""
    import re
    text = text.strip()
    match = re.search(r'\d+', text)
    if match:
        return int(match.group())
    return None

def parse_price(text: str) -> float | None:
    """Parse price from '250', '250MVR', 'MVR250', '250 mvr', '250.00'"""
    import re
    text = text.strip().upper().replace(",", "")
    text = text.replace("MVR", "").replace("RF", "").replace("MRF", "").strip()
    try:
        return float(text)
    except ValueError:
        match = re.search(r'[\d.]+', text)
        if match:
            try:
                return float(match.group())
            except ValueError:
                pass
    return None

def parse_date_flexible(text: str):
    """Parse date from many formats"""
    from datetime import datetime as _dt
    text = text.strip()
    formats = [
        "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y",
        "%Y-%m-%d",  # ISO format
        "%d-%m-%y", "%d/%m/%y",  # short year
    ]
    for fmt in formats:
        try:
            return _dt.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

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
        # Fleet: multiple boats per operator
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS boats (
                id SERIAL PRIMARY KEY,
                operator_id INTEGER REFERENCES operators(id) ON DELETE CASCADE,
                boat_name TEXT NOT NULL,
                capacity INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Daily schedule overrides (boat swap, time change, cancellation)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schedule_changes (
                id SERIAL PRIMARY KEY,
                schedule_id INTEGER REFERENCES schedules(id) ON DELETE CASCADE,
                change_date DATE NOT NULL,
                new_boat_name TEXT,
                new_time TEXT,
                note TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Add columns to schedules if missing
        await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
        await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
        await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
        # Add columns to bookings if missing
        await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE")
        # Settings table for admin-configurable values
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
        # Insert defaults
        await conn.execute("""
            INSERT INTO settings (key, value) VALUES ('samuga_logo_url', '')
            ON CONFLICT (key) DO NOTHING
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

async def get_setting(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
    return row["value"] if row and row["value"] else default

async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES ($1,$2,NOW())
            ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=NOW()
        """, key, value)

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
SAMUGA_LOGO_URL = "https://res.cloudinary.com/dfhj3clbh/image/upload/samuga_travels/logos/logo_{user_id}"

async def generate_ticket_pdf(booking: dict, operator: dict, schedule: dict) -> bytes:
    samuga_logo_url = await get_setting("samuga_logo_url", "")
    from reportlab.platypus import HRFlowable, KeepTogether
    from reportlab.lib.colors import HexColor
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()

    # Color palette — Samuga Travels ocean theme
    ST_NAVY   = HexColor('#0D2137')   # deep navy
    ST_BLUE   = HexColor('#1B6CA8')   # samuga blue
    ST_LIGHT  = HexColor('#E8F4FD')   # very light blue bg
    ST_ACCENT = HexColor('#00B4D8')   # bright accent
    ST_WHITE  = HexColor('#FFFFFF')
    ST_GRAY   = HexColor('#F5F8FA')
    ST_TEXT   = HexColor('#1A2733')
    ST_MUTED  = HexColor('#6B8A9E')

    doc = SimpleDocTemplate(buf, pagesize=A4,
                            rightMargin=15*mm, leftMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)
    styles = getSampleStyleSheet()
    story = []

    # ── HEADER BAND ──────────────────────────────────────────────────────────
    # Samuga logo top-left + operator logo top-right in a side-by-side table
    header_left_content = []
    try:
        st_logo_resp = requests.get(
            "https://res.cloudinary.com/dfhj3clbh/image/upload/samuga_travels/logos/logo_{}.png".format(
                operator.get("telegram_id", "default")), timeout=4)
        # Use operator logo as main, we'll add ST watermark text
        op_img = RLImage(io.BytesIO(st_logo_resp.content), width=22*mm, height=22*mm)
    except:
        op_img = None

    # Samuga Travels logo top-left (from settings)
    st_logo_img = None
    if samuga_logo_url:
        try:
            resp = requests.get(samuga_logo_url, timeout=5)
            st_logo_img = RLImage(io.BytesIO(resp.content), width=22*mm, height=22*mm)
        except: pass

    # Operator logo top-right
    op_logo_img = None
    if operator.get("logo_url"):
        try:
            resp = requests.get(operator["logo_url"], timeout=5)
            op_logo_img = RLImage(io.BytesIO(resp.content), width=28*mm, height=28*mm)
        except: pass

    # Header row: ST branding left, operator logo right
    st_brand = Paragraph(
        '<font color="#00B4D8"><b>SAMUGA</b></font><font color="#1B6CA8"><b>TRAVELS</b></font>',
        ParagraphStyle('stb', fontName='Helvetica-Bold', fontSize=13, alignment=0))
    st_sub = Paragraph(
        '<font color="#6B8A9E" size="7">Official Travel Partner · Maldives</font>',
        ParagraphStyle('sts', fontName='Helvetica', fontSize=7, alignment=0))

    op_name_p = Paragraph(
        f'<font color="#0D2137"><b>{operator.get("business_name","")}</b></font>',
        ParagraphStyle('opn', fontName='Helvetica-Bold', fontSize=11, alignment=2))
    op_contact_p = Paragraph(
        f'<font color="#6B8A9E" size="8">{operator.get("owner_contact","")}</font>',
        ParagraphStyle('opc', fontName='Helvetica', fontSize=8, alignment=2))

    left_cell = [[st_logo_img] if st_logo_img else [], [st_brand], [st_sub]]
    right_cell_content = []
    if op_logo_img:
        right_cell_content.append(op_logo_img)
    right_cell_content.append(op_name_p)
    right_cell_content.append(op_contact_p)

    header_table = Table([[left_cell, right_cell_content]], colWidths=[90*mm, 85*mm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ALIGN', (0,0), (0,0), 'LEFT'),
        ('ALIGN', (1,0), (1,0), 'RIGHT'),
        ('PADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(header_table)
    story.append(HRFlowable(width="100%", thickness=2, color=ST_ACCENT, spaceAfter=4*mm))

    # ── TICKET TITLE BAND ────────────────────────────────────────────────────
    title_data = [["  BOARDING TICKET  ·  " + booking["booking_ref"]]]
    title_t = Table(title_data, colWidths=[175*mm])
    title_t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), ST_NAVY),
        ('TEXTCOLOR', (0,0), (-1,-1), ST_WHITE),
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 13),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('PADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 6),
    ]))
    story.append(title_t)
    story.append(Spacer(1, 4*mm))

    # ── JOURNEY DETAILS ──────────────────────────────────────────────────────
    route_str = f"{schedule.get('route_from','')} → {schedule.get('route_to','')}"
    travel_date = str(booking.get("travel_date",""))
    dep_time = schedule.get("departure_time","")
    location = schedule.get("location","Jetty No. 1, Male")
    boat_name = operator.get("boat_name","N/A")

    # Two-column journey card
    lbl = ParagraphStyle('lbl', fontName='Helvetica-Bold', fontSize=8, textColor=ST_MUTED)
    val = ParagraphStyle('val', fontName='Helvetica-Bold', fontSize=11, textColor=ST_TEXT)
    val_sm = ParagraphStyle('vsm', fontName='Helvetica', fontSize=10, textColor=ST_TEXT)

    journey_data = [
        [Paragraph("ROUTE", lbl), Paragraph("DATE", lbl),
         Paragraph("DEPARTURE", lbl), Paragraph("LOCATION", lbl)],
        [Paragraph(route_str, val), Paragraph(travel_date, val),
         Paragraph(dep_time, val), Paragraph(location, val_sm)],
    ]
    journey_t = Table(journey_data, colWidths=[52*mm, 38*mm, 35*mm, 50*mm])
    journey_t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), ST_LIGHT),
        ('ROWBACKGROUNDS', (0,0), (-1,-1), [ST_GRAY, ST_WHITE]),
        ('BOX', (0,0), (-1,-1), 1, ST_ACCENT),
        ('LINEABOVE', (0,1), (-1,1), 1, ST_ACCENT),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(journey_t)
    story.append(Spacer(1, 3*mm))

    # ── BOAT + PAYMENT ROW ───────────────────────────────────────────────────
    boat_payment_data = [
        [Paragraph("VESSEL", lbl), Paragraph("PASSENGERS", lbl), Paragraph("TOTAL PAID", lbl)],
        [Paragraph(f"🚤 {boat_name}", val_sm),
         Paragraph(str(booking.get("passenger_count",0)), val),
         Paragraph(f"MVR {booking.get('total_amount','0')}", val)],
    ]
    bp_t = Table(boat_payment_data, colWidths=[65*mm, 50*mm, 60*mm])
    bp_t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), ST_BLUE),
        ('TEXTCOLOR', (0,0), (-1,0), ST_WHITE),
        ('BACKGROUND', (0,1), (-1,1), ST_WHITE),
        ('BOX', (0,0), (-1,-1), 1, ST_BLUE),
        ('PADDING', (0,0), (-1,-1), 8),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
    ]))
    story.append(bp_t)
    story.append(Spacer(1, 4*mm))

    # ── PASSENGER TABLE ──────────────────────────────────────────────────────
    passengers = booking.get("passengers", [])
    if isinstance(passengers, str):
        try: passengers = json.loads(passengers)
        except: passengers = []

    if passengers:
        story.append(Paragraph("PASSENGER MANIFEST",
            ParagraphStyle('pmt', fontName='Helvetica-Bold', fontSize=9,
                           textColor=ST_NAVY, spaceBefore=2, spaceAfter=3)))
        pax_data = [[
            Paragraph("#", ParagraphStyle('ph', fontName='Helvetica-Bold', fontSize=8, textColor=ST_WHITE)),
            Paragraph("FULL NAME", ParagraphStyle('ph', fontName='Helvetica-Bold', fontSize=8, textColor=ST_WHITE)),
            Paragraph("ID / PASSPORT", ParagraphStyle('ph', fontName='Helvetica-Bold', fontSize=8, textColor=ST_WHITE)),
        ]]
        for i, p in enumerate(passengers, 1):
            row_bg = ST_LIGHT if i % 2 == 0 else ST_WHITE
            pax_data.append([
                Paragraph(str(i), ParagraphStyle('pv', fontName='Helvetica-Bold', fontSize=9, textColor=ST_BLUE)),
                Paragraph(p.get("name",""), ParagraphStyle('pv2', fontName='Helvetica', fontSize=9, textColor=ST_TEXT)),
                Paragraph(p.get("id_number",""), ParagraphStyle('pv3', fontName='Helvetica', fontSize=9, textColor=ST_TEXT)),
            ])
        pax_t = Table(pax_data, colWidths=[12*mm, 95*mm, 68*mm])
        pax_t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), ST_NAVY),
            ('ROWBACKGROUNDS', (0,1), (-1,-1), [ST_WHITE, ST_LIGHT]),
            ('BOX', (0,0), (-1,-1), 0.5, ST_BLUE),
            ('INNERGRID', (0,0), (-1,-1), 0.3, HexColor('#D0E8F5')),
            ('PADDING', (0,0), (-1,-1), 7),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        story.append(pax_t)
        story.append(Spacer(1, 4*mm))

    # ── CONTACT + FOOTER ─────────────────────────────────────────────────────
    story.append(HRFlowable(width="100%", thickness=1, color=ST_ACCENT, spaceBefore=2, spaceAfter=3*mm))

    contact_text = (
        f"<b>Operator Contact:</b> {operator.get('owner_contact','N/A')} &nbsp;|&nbsp; "
        f"<b>Business:</b> {operator.get('business_name','')} &nbsp;|&nbsp; "
        f"<b>Questions?</b> Contact your operator or Samuga Travels"
    )
    story.append(Paragraph(contact_text,
        ParagraphStyle('ct', fontName='Helvetica', fontSize=8,
                       textColor=ST_MUTED, alignment=TA_CENTER, spaceAfter=2)))

    story.append(Paragraph(
        "✅ <b>Present this ticket when boarding.</b> This is an official Samuga Travels booking ticket.",
        ParagraphStyle('f1', fontName='Helvetica', fontSize=8,
                       textColor=ST_TEXT, alignment=TA_CENTER, spaceAfter=1)))

    story.append(Paragraph(
        f"<font color='#1B6CA8'><b>Samuga Travels</b></font> · Maldives · Issued {datetime.now().strftime('%d %b %Y %H:%M')} MVT",
        ParagraphStyle('f2', fontName='Helvetica', fontSize=7,
                       textColor=ST_MUTED, alignment=TA_CENTER)))

    doc.build(story)
    return buf.getvalue()

# ── KEYBOARDS ─────────────────────────────────────────────────────────────────
def main_kb(role="customer"):
    if role == "operator":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 My Profile",       callback_data="op_profile"),
             InlineKeyboardButton("🗓️ Add Schedule",     callback_data="op_schedules")],
            [InlineKeyboardButton("🚤 My Fleet",         callback_data="op_fleet"),
             InlineKeyboardButton("📦 Pending Bookings", callback_data="op_bookings")],
            [InlineKeyboardButton("✏️ Edit Info",        callback_data="op_edit"),
             InlineKeyboardButton("📅 Today's Schedule", callback_data="op_today")],
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

# ── ADMIN COMMANDS ────────────────────────────────────────────────────────────
def is_admin(user_id: int, chat_id: int) -> bool:
    return user_id in SUPER_ADMINS or chat_id == ADMIN_GROUP_ID

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Main admin dashboard — /admin"""
    user = update.effective_user
    if not is_admin(user.id, update.effective_chat.id):
        await update.message.reply_text("⛔ Admin only.")
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        total_ops     = await conn.fetchval("SELECT COUNT(*) FROM operators WHERE status='approved'")
        pending_ops   = await conn.fetchval("SELECT COUNT(*) FROM operators WHERE status='pending'")
        total_bookings= await conn.fetchval("SELECT COUNT(*) FROM bookings")
        confirmed_bk  = await conn.fetchval("SELECT COUNT(*) FROM bookings WHERE status='confirmed'")
        total_revenue = await conn.fetchval("SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed'")
        total_customers = await conn.fetchval("SELECT COUNT(DISTINCT customer_telegram_id) FROM bookings")

    samuga_logo = await get_setting("samuga_logo_url", "")

    msg = (
        f"🛠️ *Samuga Travels — Admin Panel*\n\n"
        f"📊 *Platform Stats:*\n"
        f"  ✅ Approved Operators: *{total_ops}*\n"
        f"  ⏳ Pending Review: *{pending_ops}*\n"
        f"  🎫 Total Bookings: *{total_bookings}* ({confirmed_bk} confirmed)\n"
        f"  👥 Unique Customers: *{total_customers}*\n"
        f"  💰 Total Revenue: *MVR {total_revenue:.2f}*\n\n"
        f"🖼️ Samuga Logo: {'✅ Set' if samuga_logo else '❌ Not set'}\n\n"
        f"Choose an action below:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 Manage Operators",    callback_data="adm_operators"),
         InlineKeyboardButton("📦 All Bookings",        callback_data="adm_bookings")],
        [InlineKeyboardButton("📢 Broadcast Message",  callback_data="adm_broadcast"),
         InlineKeyboardButton("📊 Revenue Report",     callback_data="adm_revenue")],
        [InlineKeyboardButton("🖼️ Upload Samuga Logo", callback_data="adm_upload_logo"),
         InlineKeyboardButton("⚙️ Settings",           callback_data="adm_settings")],
        [InlineKeyboardButton("🔍 Find Customer",      callback_data="adm_find_customer"),
         InlineKeyboardButton("🚤 All Schedules",      callback_data="adm_schedules")],
    ])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=kb)

async def admin_check(query, ctx) -> bool:
    """Check admin access for callbacks"""
    user = query.from_user
    if not is_admin(user.id, query.message.chat.id):
        await query.answer("⛔ Admin only.", show_alert=True)
        return False
    return True

async def cmd_urgent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Operator sends urgent review request"""
    user = update.effective_user
    op = await get_operator(user.id)
    if not op:
        await update.message.reply_text("⚠️ You don't have a pending application.")
        return
    if op["status"] == "approved":
        await update.message.reply_text("✅ Your account is already approved!")
        return
    if op["status"] == "rejected":
        await update.message.reply_text("❌ Your application was rejected. Contact @SamugaTravels.")
        return

    # Notify admin group with urgent flag
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM operators WHERE telegram_id=$1", user.id)
    op_id = row["id"] if row else 0

    urgent_msg = (
        f"🚨 *URGENT REVIEW REQUEST*\n\n"
        f"👤 @{user.username or user.first_name} (`{user.id}`)\n"
        f"🏢 *{op['business_name']}*\n"
        f"🛥️ {op['boat_name']}\n\n"
        f"⚡ Operator is requesting urgent approval."
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve Now", callback_data=f"approve_op_{op_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject_op_{op_id}")
    ]])
    try:
        await ctx.bot.send_message(ADMIN_GROUP_ID, urgent_msg, parse_mode="Markdown",
                                   message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
        await update.message.reply_text(
            "🚨 *Urgent request sent!*\n\n"
            "Our team has been notified and will review your application as soon as possible.\n\n"
            "Thank you for your patience! 🙏",
            parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Urgent notify error: {e}")
        await update.message.reply_text("⚠️ Could not send request. Please contact @SamugaTravels directly.")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Check application status"""
    user = update.effective_user
    op = await get_operator(user.id)
    if not op:
        await update.message.reply_text(
            "📋 You don't have an operator application.\n\nTap below to register!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🤝 Register as Operator", callback_data="register_operator")
            ]]))
        return
    status_map = {
        "pending":  ("⏳", "Under Review", "Our team is reviewing your application."),
        "approved": ("✅", "Approved", "Your account is active. Use /start to manage."),
        "rejected": ("❌", "Rejected", "Contact @SamugaTravels for more info.")
    }
    icon, label, note = status_map.get(op["status"], ("❓","Unknown",""))
    rec = "🌟 *Recommended by Samuga Travels*\n" if op.get("is_recommended") else ""
    await update.message.reply_text(
        f"{icon} *Application Status: {label}*\n\n"
        f"🏢 {op['business_name']}\n"
        f"🛥️ {op['boat_name']}\n"
        f"{rec}"
        f"\n_{note}_\n\n"
        f"{'Type /urgent if you need urgent review.' if op['status'] == 'pending' else ''}",
        parse_mode="Markdown")

async def cmd_findcustomer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Find customer by booking ref or telegram ID"""
    user = update.effective_user
    if not is_admin(user.id, update.effective_chat.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    args = ctx.args
    if not args:
        await update.message.reply_text("Usage: `/findcustomer ST-260629-0389` or `/findcustomer 123456789`", parse_mode="Markdown")
        return
    query_str = args[0].strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        if query_str.startswith("ST-"):
            bk = await conn.fetchrow("""
                SELECT b.*, o.business_name FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                WHERE b.booking_ref=$1
            """, query_str)
        else:
            try:
                tg_id = int(query_str)
                bk = await conn.fetchrow("""
                    SELECT b.*, o.business_name FROM bookings b
                    JOIN operators o ON b.operator_id=o.id
                    WHERE b.customer_telegram_id=$1 ORDER BY b.created_at DESC LIMIT 1
                """, tg_id)
            except ValueError:
                await update.message.reply_text("⚠️ Invalid format. Use booking ref or Telegram ID.")
                return
    if not bk:
        await update.message.reply_text("❌ No booking found.")
        return
    icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
    ic = icons.get(bk["status"],"❓")
    msg = (
        f"🔍 *Booking Found:*\n\n"
        f"{ic} `{bk['booking_ref']}`\n"
        f"👤 Customer: {bk['customer_name'] or 'N/A'}\n"
        f"🆔 Telegram ID: `{bk['customer_telegram_id']}`\n"
        f"🚤 Operator: {bk['business_name']}\n"
        f"📅 {bk['travel_date']} | 💰 MVR {bk['total_amount']}\n"
        f"📊 Status: *{bk['status'].upper()}*\n"
        f"🕐 Created: {str(bk['created_at'])[:16]}"
    )
    btns = [[InlineKeyboardButton("✉️ Message Customer", callback_data=f"msg_customer_{bk['customer_telegram_id']}")]]
    if bk["status"] == "pending_confirmation":
        btns.append([InlineKeyboardButton("✅ Force Confirm", callback_data=f"confirm_booking_{bk['id']}")])
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

async def cmd_ops(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """List all approved operators"""
    user = update.effective_user
    if user.id not in SUPER_ADMINS and update.effective_chat.id != ADMIN_GROUP_ID:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        ops = await conn.fetch("SELECT id, business_name, boat_name, status, telegram_id FROM operators ORDER BY status, business_name")
    if not ops:
        await update.message.reply_text("No operators.")
        return
    msg = "📋 *All Operators:*\n\n"

    for op in ops:
        icon = {"pending":"⏳","approved":"✅","rejected":"❌"}.get(op["status"],"❓")
        msg += f"{icon} `{op['id']}` *{op['business_name']}* — {op['boat_name']}\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

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
            return
        elif s == "pending":
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("🚨 Request Urgent Review", callback_data=f"urgent_review_{existing['id']}")
            ]])
            await msg.reply_text(
                "⏳ *Your application is under review.*\n\n"
                "Our team will notify you once approved.\n\n"
                "Need it urgently? Tap the button below to flag your application:",
                parse_mode="Markdown", reply_markup=kb)
            return
        elif s == "rejected":
            # Allow re-registration after rejection
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("DELETE FROM operators WHERE telegram_id=$1", user.id)
            # Fall through to registration below

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

    # ── ADMIN MESSAGE STATES ─────────────────────────────────────────────────
    if state == ADMIN_AWAIT_BROADCAST:
        if is_cancel(text):
            await set_user_state(user.id, CX_IDLE, {})
            await update.message.reply_text("❌ Broadcast cancelled.")
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            operators = await conn.fetch("SELECT telegram_id, business_name FROM operators WHERE status='approved'")
        sent = 0
        failed = 0
        for op in operators:
            try:
                await ctx.bot.send_message(op["telegram_id"],
                    f"📢 *Message from Samuga Travels:*\n\n{text}",
                    parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        await set_user_state(user.id, CX_IDLE, {})
        await update.message.reply_text(
            f"📢 *Broadcast Complete!*\n\n✅ Sent: {sent}\n❌ Failed: {failed}",
            parse_mode="Markdown")
        return

    elif state == ADMIN_AWAIT_REVIEW_TEXT:
        op_id = (sd.get("temp_data") or {}).get("review_op_id")
        if op_id and not is_cancel(text):
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE operators SET is_recommended=TRUE, review_text=$1 WHERE id=$2 RETURNING telegram_id, business_name",
                    text, op_id)
            if row:
                await ctx.bot.send_message(row["telegram_id"],
                    f"🌟 *Congratulations!*\n\nYour business is now *Recommended by Samuga Travels!*\n\n💬 _{text}_",
                    parse_mode="Markdown")
                await update.message.reply_text(f"🌟 *{row['business_name']}* is now Recommended!", parse_mode="Markdown")
        await set_user_state(user.id, CX_IDLE, {})
        return

    # ── GLOBAL CANCEL CHECK ──────────────────────────────────────────────────
    if is_cancel(text) and state not in [CX_IDLE, OP_IDLE]:
        role = sd.get("role","customer")
        if role == "operator":
            op = await get_operator(user.id)
            role = "operator" if (op and op.get("status")=="approved") else "customer"
        await set_user_state(user.id, CX_IDLE if role != "operator" else OP_IDLE, {})
        await update.message.reply_text(
            "❌ Cancelled. Back to main menu.",
            reply_markup=main_kb(role))
        return

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
        seat_num = parse_number(text)
        if not seat_num or seat_num < 1:
            await update.message.reply_text("⚠️ Please enter a valid number e.g. `20`", parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_AWAIT_TYPE, {**temp, "seat_count": seat_num})
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
        if is_skip(text):
            mib_entry = ""
        else:
            parts = text.strip().split(" ", 1)
            acct_num = parts[0].strip()
            acct_name = parts[1].strip() if len(parts) > 1 else ""
            mib_entry = f"{acct_num}|{acct_name}" if acct_name else acct_num
        final_temp = {**temp, "mib_account": mib_entry}
        op_id = await save_operator(user, final_temp)
        await notify_admin_new_op(ctx, user, final_temp, op_id=op_id)
        await set_user_state(user.id, OP_REGISTERED, {})
        await update.message.reply_text(
            "🎉 *Registration Complete!*\n\n"
            "Your application has been submitted to Samuga Travels for review.\n\n"
            "⏳ We\'ll verify your details and notify you here within 24 hours. Thank you! 🌊",
            parse_mode="Markdown")

    # ── FLEET / BOAT ADD FLOW ────────────────────────────────────────────────────
    elif state == OP_AWAIT_BOAT_ADD_NAME:
        boat_name = text.strip()
        await set_user_state(user.id, OP_AWAIT_BOAT_ADD_CAPACITY, {**temp, "new_boat_name": boat_name})
        await update.message.reply_text(
            f"🚤 *{boat_name}*\n\nHow many passengers can this boat carry?",
            parse_mode="Markdown")

    elif state == OP_AWAIT_BOAT_ADD_CAPACITY:
        capacity = parse_number(text)
        if not capacity or capacity < 1:
            await update.message.reply_text("⚠️ Enter a valid number e.g. `20`", parse_mode="Markdown")
            return
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO boats (operator_id, boat_name, capacity) VALUES ($1,$2,$3)",
                op["id"], temp.get("new_boat_name"), capacity)
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        await set_user_state(user.id, OP_IDLE, {})
        fleet_list = "\n".join([f"  🚤 {b['boat_name']} ({b['capacity']} seats)" for b in boats])
        await update.message.reply_text(
            f"✅ *{temp.get('new_boat_name')}* added to your fleet!\n\n"
            f"*Your Fleet:*\n{fleet_list}",
            parse_mode="Markdown", reply_markup=main_kb("operator"))

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
        price = parse_price(text)
        if price is None or price <= 0:
            await update.message.reply_text("⚠️ Enter a valid price e.g. `535` or `535MVR`", parse_mode="Markdown")
            return
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_SEATS, {**temp, "sched_price": price})
        await update.message.reply_text("✅ Price saved!\n\nHow many *available seats* for this schedule?",
                                        parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_SEATS:
        seats = parse_number(text)
        if not seats or seats < 1:
            await update.message.reply_text("⚠️ Enter a valid number e.g. `18`", parse_mode="Markdown")
            return
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
        # Ask for location next
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_LOCATION,
                             {**t2, "sched_seats": seats})
        await update.message.reply_text(
            f"✅ {seats} seats saved!\n\n"
            f"📍 *What is the departure location/jetty?*\n\n"
            f"_Example: Jetty No. 1, Male_ or _Thoddoo Jetty_",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_LOCATION:
        location = text.strip() or "Jetty No. 1, Male"
        await set_user_state(user.id, OP_AWAIT_SCHEDULE_DAYS, {**temp, "sched_location": location})
        await update.message.reply_text(
            f"✅ Location: *{location}*\n\n"
            f"📅 *Which days does this schedule run?*\n\n"
            f"_Type one of:_\n"
            f"• `daily` — Every day\n"
            f"• `sat-thu` — Saturday to Thursday\n"
            f"• `fri` — Fridays only\n"
            f"• `weekdays` — Sunday to Thursday\n"
            f"• `weekend` — Friday & Saturday",
            parse_mode="Markdown")

    elif state == OP_AWAIT_SCHEDULE_DAYS:
        days_input = text.strip().lower()
        valid_days = ["daily","sat-thu","fri","weekdays","weekend","sun-thu","everyday"]
        run_days = days_input if days_input in valid_days else "daily"
        t2 = temp
        op = await get_operator(user.id)
        # Get operator's boats
        pool = await get_pool()
        async with pool.acquire() as conn:
            boats_list = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        if boats_list:
            # Let operator pick which boat runs this schedule
            boat_buttons = [[InlineKeyboardButton(f"🚤 {b['boat_name']} ({b['capacity']} seats)",
                callback_data=f"sched_boat_{b['id']}_{b['boat_name']}")] for b in boats_list]
            boat_buttons.append([InlineKeyboardButton("➕ Use Default (no specific boat)", callback_data="sched_boat_0_default")])
            # Save days in state first
            import json as _j
            await set_user_state(user.id, OP_AWAIT_SCHEDULE_DAYS,
                                 {**t2, "sched_location": t2.get("sched_location","Jetty No. 1, Male"),
                                  "run_days": run_days, "awaiting_boat_select": True})
            await update.message.reply_text(
                f"✅ Days: *{run_days}*\n\n🚤 *Which boat runs this schedule?*",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(boat_buttons))
        else:
            # No boats added yet — save directly
            import json as _j
            seats = t2.get("sched_seats", t2.get("sched_price",0))
            async with pool.acquire() as conn:
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'")
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
                await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
                await conn.execute("""
                    INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                           price_per_seat, total_seats, available_seats,
                                           sched_stops, location, run_days)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                    t2.get("sched_time"), t2.get("sched_price"),
                    t2.get("sched_seats",0), t2.get("sched_seats",0),
                    _j.dumps(t2.get("sched_stops",[])),
                    t2.get("sched_location","Jetty No. 1, Male"), run_days)
            await set_user_state(user.id, OP_IDLE, {})
            await update.message.reply_text(
                f"✅ *Schedule Added!*\n\n"
                f"📍 {t2.get('sched_from')} → {t2.get('sched_to')}\n"
                f"⏰ {t2.get('sched_time')} | 📅 {run_days}\n"
                f"📌 {t2.get('sched_location','Jetty No. 1, Male')}\n"
                f"💰 MVR {t2.get('sched_price')}/seat | 👥 {t2.get('sched_seats',0)} seats\n\n"
                f"💡 Tip: Add your boats with the *🚤 My Fleet* button!",
                parse_mode="Markdown", reply_markup=main_kb("operator"))

    # ── CUSTOMER FLOW ─────────────────────────────────────────────────────────
    elif state == CX_AWAIT_DATE:
        travel_date = parse_date_flexible(text)
        if not travel_date:
            await update.message.reply_text(
                "⚠️ Couldn\'t read that date 😅\n\nTry formats like:\n`30-06-2026` or `30/06/2026`",
                parse_mode="Markdown")
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
        # Store only IDs in state to avoid temp_data size limit; cache full data in context
        sched_ids = [s["id"] for s in schedules]
        ctx.user_data["schedules_cache"] = schedules
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT,
                             {**temp, "travel_date": str(travel_date), "sched_ids": sched_ids})

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
        await update.message.reply_text(
            f"✅ *{cx_name}* saved!\n\n"
            f"💺 How many seats would you like to book?\n_(Max 10, available: {temp.get('sel_seats',0)})_",
            parse_mode="Markdown")

    elif state == CX_AWAIT_PASSENGER_COUNT:
        count = parse_number(text)
        if not count or count < 1:
            await update.message.reply_text("⚠️ Enter a valid number e.g. `2`", parse_mode="Markdown")
            return
        if count > 10:
            await update.message.reply_text("⚠️ Maximum 10 seats per booking.")
            return
        if count > int(temp.get("sel_seats", 0)):
            await update.message.reply_text(f"⚠️ Only *{temp.get('sel_seats')} seats* available.", parse_mode="Markdown")
            return
        # Build example format based on count
        example_lines = "\n".join([f"{i+1}. Full Name, ID Number" for i in range(count)])
        example_filled = "\n".join([
            "1. Ahmed Ali, A123456",
            "2. Fatima Mohamed, A654321",
            "3. Hassan Ali, A789012"
        ][:count])
        await set_user_state(user.id, CX_COLLECTING_PASSENGERS,
                             {**temp, "passenger_count": count, "passengers_collected": [], "current_passenger": 1})
        await update.message.reply_text(
            f"👥 *Enter all {count} passenger(s) at once:*\n\n"
            f"_Format — one per line:_\n`Name, ID Number`\n\n"
            f"_Example:_\n`{example_filled}`\n\n"
            f"Send all {count} passengers in one message 👇",
            parse_mode="Markdown")

    elif state == CX_COLLECTING_PASSENGERS:
        sd2 = await get_user_state(user.id)
        t2  = sd2.get("temp_data", {}) or {}
        total = t2.get("passenger_count", 1)

        # Parse all passengers from one message — one per line
        # Strip leading numbers like "1. Ahmed" or "1) Ahmed"
        import re as _re
        lines_raw = [_re.sub(r"^\d+[.)\-\s]+", "", l.strip()) for l in text.strip().split("\n") if l.strip()]
        passengers = []
        errors = []
        for i, line in enumerate(lines_raw):
            parsed = parse_name_id(line)
            if parsed:
                passengers.append({"name": parsed[0], "id_number": parsed[1]})
            else:
                errors.append(f"Line {i+1}: couldn\'t read `{line}`")

        if errors or len(passengers) != total:
            example = "\n".join([f"{i+1}. Ahmed Ali, A12345{i}" for i in range(total)])
            err_msg = "\n".join(errors) if errors else ""
            await update.message.reply_text(
                f"⚠️ Need exactly *{total} passenger(s)*, one per line.\n\n"
                f"{err_msg}\n\n"
                f"_Example for {total} passenger(s):_\n`{example}`",
                parse_mode="Markdown")
            return

        if True:  # always show summary now
            t2["passengers_collected"] = passengers
            # fall through to summary below
            pass
        if False:
            pass
        else:
            sd3 = await get_user_state(user.id)
            t3  = sd3.get("temp_data", {}) or {}
            t3["passengers_collected"] = passengers
            total_amt = float(t3.get("sel_price", 0)) * total
            pax_lines = "\n".join([f"  {i+1}. {p['name']} ({p['id_number']})" for i,p in enumerate(passengers)])
            import json as _json
            pay_str = ""
            try:
                accounts = _json.loads(t3.get("sel_payment_accounts") or "[]")
                if accounts:
                    for acc in accounts:
                        pay_str += f"🏦 *{acc['bank']}:* `{acc['number']}`"
                        if acc.get("name"): pay_str += f" — {acc['name']}"
                        pay_str += "\n"
                else:
                    pay_str = f"🏦 *BML:* `{t3.get('sel_bml','N/A')}`\n"
            except Exception:
                pay_str = f"🏦 *BML:* `{t3.get('sel_bml','N/A')}`\n"

            summary = (
                f"📝 *Booking Summary*\n\n"
                f"👤 *Booker:* {t3.get('cx_name','N/A')} | 📞 {t3.get('cx_phone','N/A')}\n"
                f"🚤 *Operator:* {t3.get('sel_business')}\n"
                f"🛥️ *Boat:* {t3.get('sel_boat')}\n"
                f"📍 *Route:* {t3.get('route_from')} → {t3.get('route_to')}\n"
                f"📅 *Date:* {t3.get('travel_date')}\n"
                f"⏰ *Departure:* {t3.get('sel_time')}\n"
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
            from datetime import timedelta
            today = datetime.now().date()
            dates = [today + timedelta(days=i) for i in range(4)]
            date_buttons = [[InlineKeyboardButton(
                f"{'Today' if i==0 else 'Tomorrow' if i==1 else d.strftime('%a %d %b')}",
                callback_data=f"date_select_{d.strftime('%d-%m-%Y')}"
            )] for i, d in enumerate(dates)]
            await update.message.reply_text(
                f"🔍 *{rf} → {rt}*\n\n📅 Select your *travel date* or type manually:\n_(DD-MM-YYYY or DD/MM/YYYY)_",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(date_buttons))
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
        try:
            ref = gen_ref()
            url = await upload_image(file_bytes, "payment_slips", f"slip_{ref}")
            sd2 = await get_user_state(user.id)
            t2  = sd2.get("temp_data", {}) or {}

            # Safe type casting
            from datetime import date as _date
            travel_date_raw = t2.get("travel_date","")
            try:
                if isinstance(travel_date_raw, str) and travel_date_raw:
                    travel_date_val = datetime.strptime(travel_date_raw, "%Y-%m-%d").date()
                else:
                    travel_date_val = _date.today()
            except Exception:
                travel_date_val = _date.today()

            operator_id  = int(t2.get("sel_operator_id") or 0) or None
            schedule_id  = int(t2.get("sel_schedule_id") or 0) or None
            pax_count    = int(t2.get("passenger_count") or 1)
            total_amount = float(t2.get("total_amount") or 0)
            customer_name = f"{t2.get('cx_name','')} | {t2.get('cx_phone','')}"
            passengers_json = json.dumps(t2.get("passengers_collected",[]))

            logger.info(f"Booking insert: ref={ref} op={operator_id} sched={schedule_id} date={travel_date_val} pax={pax_count} amt={total_amount}")

            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO bookings (booking_ref, customer_telegram_id, customer_name, operator_id, schedule_id,
                                          travel_date, passenger_count, passengers, total_amount,
                                          payment_slip_url, status)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'pending_confirmation')
                    RETURNING id
                """, ref, user.id, customer_name, operator_id, schedule_id,
                    travel_date_val, pax_count, passengers_json, total_amount, url)
            booking_id = row["id"]
            logger.info(f"✅ Booking {ref} saved with id={booking_id}")

            await set_user_state(user.id, CX_BOOKING_COMPLETE, {"booking_ref": ref, "booking_id": booking_id})
            await update.message.reply_text(
                f"✅ *Payment slip received!*\n\n"
                f"📋 Booking Ref: `{ref}`\n\n"
                f"Your booking is being reviewed by the operator. "
                f"You will receive your confirmed ticket within *5-10 minutes*. "
                f"Please do not resend your slip - we have received it!",
                parse_mode="Markdown")

            sel = {
                "operator_id": operator_id,
                "id": schedule_id,
                "departure_time": t2.get("sel_time",""),
                "op_telegram_id": t2.get("sel_op_tg", 0),
            }
            await notify_operator_payment(ctx, booking_id, sel, t2, ref, user, photo.file_id)

        except Exception as e:
            logger.error(f"❌ Payment slip error: {e}", exc_info=True)
            await update.message.reply_text(
                "⚠️ Sorry, something went wrong saving your booking.\n\n"
                "Don\'t worry — please send your payment slip directly to the operator "
                "and they will confirm manually. We apologise for the inconvenience! 🙏",
                parse_mode="Markdown")


    elif state == ADMIN_AWAIT_LOGO:
        if not is_admin(user.id, update.effective_chat.id):
            await update.message.reply_text("⛔ Admin only.")
            return
        await update.message.reply_text("⏳ Uploading Samuga Travels logo...")
        url = await upload_image(file_bytes, "branding", "samuga_travels_logo")
        await set_setting("samuga_logo_url", url)
        await set_user_state(user.id, CX_IDLE, {})
        await update.message.reply_text(
            f"✅ *Samuga Travels logo updated!*\n\n"
            f"It will now appear on every ticket. 🎫\n\n"
            f"URL: `{url}`",
            parse_mode="Markdown")

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

    elif data.startswith("date_select_"):
        selected_date_str = data.replace("date_select_", "")
        # Inject as if user typed the date
        sd2 = await get_user_state(user.id)
        t2 = sd2.get("temp_data", {}) or {}
        # Fake a message with this date into the state handler
        travel_date = datetime.strptime(selected_date_str, "%d-%m-%Y").date()
        route_from = t2.get("route_from","")
        route_to   = t2.get("route_to","")
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
            await query.message.reply_text(
                f"😔 No boats for *{route_from} → {route_to}* on *{selected_date_str}*. Try another date.",
                parse_mode="Markdown")
            return
        schedules = [dict(r) for r in rows]
        ctx.user_data["schedules_cache"] = schedules
        sched_ids = [s["id"] for s in schedules]
        await set_user_state(user.id, CX_AWAIT_PASSENGER_COUNT,
                             {**t2, "travel_date": str(travel_date), "sched_ids": sched_ids})
        import json as _j
        msg = f"🚢 *Available Boats — {route_from} → {route_to}*\n📅 *{selected_date_str}*\n\n"
        buttons = []
        for i, s in enumerate(schedules):
            rating_val = float(s.get("average_rating") or 0)
            stars = "⭐" * int(rating_val) if rating_val else "No ratings yet"
            rec = "✨ *Recommended by Samuga Travels*\n" if s.get("is_recommended") else ""
            try:
                stops_list = _j.loads(s.get("sched_stops") or "[]")
                stops_line = "🛑 " + " → ".join(stops_list) + "\n" if stops_list and len(stops_list) > 2 else ""
            except: stops_line = ""
            msg += (
                f"{'─'*30}\n"
                f"🚤 *{s['business_name']}* — _{s['boat_name']}_\n"
                f"{rec}"
                f"📍 {s['route_from']} → {s['route_to']}\n"
                f"{stops_line}"
                f"⏰ *{s['departure_time']}*\n"
                f"💺 {s['available_seats']} seats | 💰 MVR {s['price_per_seat']}/seat\n"
                f"⭐ {stars}\n\n"
            )
            buttons.append([InlineKeyboardButton(
                f"Book — {s['business_name']} ({s['departure_time']})",
                callback_data=f"book_sched_{i}")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

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
        schedules = ctx.user_data.get("schedules_cache", [])
        if not schedules:
            await query.message.reply_text("⚠️ Session expired. Please search again.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Search Again", callback_data="cx_search")]]))
            return
        if idx >= len(schedules):
            await query.message.reply_text("⚠️ Invalid selection.")
            return
        sel = schedules[idx]
        # Store flat keys to avoid large JSON in temp_data
        await set_user_state(user.id, CX_AWAIT_CONTACT, {
            **temp,
            "sel_operator_id": sel.get("operator_id"),
            "sel_schedule_id": sel.get("id"),
            "sel_business": sel.get("business_name"),
            "sel_boat": sel.get("boat_name"),
            "sel_time": sel.get("departure_time"),
            "sel_price": str(sel.get("price_per_seat", 0)),
            "sel_seats": int(sel.get("available_seats", 0)),
            "sel_bml": sel.get("bml_account", ""),
            "sel_payment_accounts": sel.get("payment_accounts", "[]"),
            "sel_op_tg": sel.get("op_telegram_id", 0),
            "route_from": temp.get("route_from", ""),
            "route_to": temp.get("route_to", ""),
            "travel_date": temp.get("travel_date", ""),
        })
        await query.message.reply_text(
            f"✅ *{sel['business_name']}* selected!\n\n"
            f"📍 {temp.get('route_from')} → {temp.get('route_to')}\n"
            f"⏰ {sel['departure_time']} | 💺 {sel['available_seats']} seats\n\n"
            f"👤 *Your contact details:*\nEnter *Full Name* and *Phone Number*:\n\n_Format: Ahmed Ali, 7771234_",
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

    elif data.startswith("sched_boat_"):
        # Format: sched_boat_{boat_id}_{boat_name}
        parts_data = data.split("_", 3)
        boat_id = int(parts_data[2])
        boat_name_sel = parts_data[3] if len(parts_data) > 3 else "default"
        sd2 = await get_user_state(user.id)
        t2 = sd2.get("temp_data", {}) or {}
        import json as _j
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
            await conn.execute("""
                INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                       price_per_seat, total_seats, available_seats,
                                       sched_stops, location, run_days, boat_name)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                t2.get("sched_time"), t2.get("sched_price"),
                t2.get("sched_seats",0), t2.get("sched_seats",0),
                _j.dumps(t2.get("sched_stops",[])),
                t2.get("sched_location","Jetty No. 1, Male"),
                t2.get("run_days","daily"),
                None if boat_name_sel == "default" else boat_name_sel)
        await set_user_state(user.id, OP_IDLE, {})
        boat_display = boat_name_sel if boat_name_sel != "default" else "Default"
        await query.edit_message_text(
            f"✅ *Schedule Added!*\n\n"
            f"📍 {t2.get('sched_from')} → {t2.get('sched_to')}\n"
            f"⏰ {t2.get('sched_time')} | 📅 {t2.get('run_days','daily')}\n"
            f"📌 {t2.get('sched_location','Jetty No. 1, Male')}\n"
            f"🚤 Boat: {boat_display}\n"
            f"💰 MVR {t2.get('sched_price')}/seat | 👥 {t2.get('sched_seats',0)} seats",
            parse_mode="Markdown")

    elif data == "op_fleet":
        op = await get_operator(user.id)
        if not op:
            await query.message.reply_text("⚠️ No operator profile.")
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 ORDER BY created_at", op["id"])
        if not boats:
            await query.message.reply_text(
                "🚤 *Your Fleet*\n\nNo boats added yet.\n\nAdd your first boat:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add a Boat", callback_data="op_add_boat")
                ]]))
            return
        msg = "🚤 *Your Fleet:*\n\n"
        buttons = []
        for b in boats:
            status_icon = "✅" if b["status"] == "active" else "🔧"
            msg += f"{status_icon} *{b['boat_name']}* — {b['capacity']} seats\n"
            buttons.append([
                InlineKeyboardButton(f"🔧 Maintenance — {b['boat_name']}", callback_data=f"boat_maintenance_{b['id']}"),
                InlineKeyboardButton(f"✅ Active", callback_data=f"boat_active_{b['id']}")
            ])
        buttons.append([InlineKeyboardButton("➕ Add Another Boat", callback_data="op_add_boat")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "op_add_boat":
        await set_user_state(user.id, OP_AWAIT_BOAT_ADD_NAME, {})
        await query.message.reply_text(
            "🚤 *Add a Boat*\n\nWhat is this boat's name?\n\n_Example: SamugaTravels 1, Ocean Star_",
            parse_mode="Markdown")

    elif data.startswith("boat_maintenance_"):
        boat_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE boats SET status='maintenance' WHERE id=$1 RETURNING boat_name", boat_id)
        if row:
            await query.answer(f"🔧 {row['boat_name']} set to maintenance.", show_alert=True)
            await query.edit_message_text(f"🔧 *{row['boat_name']}* is now under maintenance.\nCustomers won't see it in available boats.", parse_mode="Markdown")

    elif data.startswith("boat_active_"):
        boat_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE boats SET status='active' WHERE id=$1 RETURNING boat_name", boat_id)
        if row:
            await query.answer(f"✅ {row['boat_name']} is now active!", show_alert=True)
            await query.edit_message_text(f"✅ *{row['boat_name']}* is now active.", parse_mode="Markdown")

    elif data == "op_today":
        op = await get_operator(user.id)
        if not op:
            return
        from datetime import timedelta as _td
        today = datetime.now().date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            scheds = await conn.fetch("""
                SELECT s.*, COALESCE(sc.new_boat_name, s.boat_name) as active_boat,
                       COALESCE(sc.new_time, s.departure_time) as active_time,
                       sc.note as change_note
                FROM schedules s
                LEFT JOIN schedule_changes sc ON sc.schedule_id=s.id AND sc.change_date=$1
                WHERE s.operator_id=$2 AND s.is_active=TRUE
                ORDER BY s.departure_time
            """, today, op["id"])
            # Count bookings for each schedule today
            bookings_today = await conn.fetch("""
                SELECT schedule_id, COUNT(*) as count, SUM(passenger_count) as pax
                FROM bookings WHERE travel_date=$1 AND status='confirmed' AND operator_id=$2
                GROUP BY schedule_id
            """, today, op["id"])
        booking_map = {b["schedule_id"]: b for b in bookings_today}
        if not scheds:
            await query.message.reply_text("📅 No schedules found.")
            return
        msg = f"📅 *Today's Schedule — {today.strftime('%A, %d %b')}*\n\n"
        buttons = []
        for s in scheds:
            bk = booking_map.get(s["id"])
            pax = bk["pax"] if bk else 0
            count = bk["count"] if bk else 0
            change_note = f"\n⚠️ *Change:* {s['change_note']}" if s.get("change_note") else ""
            msg += (
                f"⏰ *{s['active_time']}* — {s['route_from']} → {s['route_to']}\n"
                f"🚤 {s['active_boat'] or 'Default boat'}\n"
                f"📌 {s.get('location','Jetty No. 1, Male')}\n"
                f"🎫 {count} bookings | 👥 {pax} passengers{change_note}\n\n"
            )
            buttons.append([InlineKeyboardButton(
                f"✏️ Change {s['active_time']} schedule",
                callback_data=f"change_sched_{s['id']}")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("change_sched_"):
        sched_id = int(data.split("_")[-1])
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        if not sched:
            await query.answer("Schedule not found.", show_alert=True)
            return
        buttons = []
        for b in boats:
            buttons.append([InlineKeyboardButton(
                f"🚤 Swap to {b['boat_name']}",
                callback_data=f"swap_boat_{sched_id}_{b['boat_name']}")])
        buttons.append([InlineKeyboardButton("⏰ Change Time", callback_data=f"swap_time_{sched_id}")])
        buttons.append([InlineKeyboardButton("❌ Cancel Today's Departure", callback_data=f"cancel_today_{sched_id}")])
        await query.message.reply_text(
            f"✏️ *Change Today's Schedule*\n\n"
            f"⏰ {sched['departure_time']} — {sched['route_from']} → {sched['route_to']}\n"
            f"📌 {sched.get('location','Jetty No. 1, Male')}\n\n"
            f"What would you like to change?",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("swap_boat_"):
        parts_s = data.split("_", 3)
        sched_id = int(parts_s[2])
        new_boat = parts_s[3]
        today = datetime.now().date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO schedule_changes (schedule_id, change_date, new_boat_name, note)
                VALUES ($1,$2,$3,'Boat swapped by operator')
                ON CONFLICT DO NOTHING
            """, sched_id, today, new_boat)
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            # Notify confirmed customers for today
            bookings = await conn.fetch("""
                SELECT customer_telegram_id, booking_ref FROM bookings
                WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
            """, sched_id, today)
        await query.edit_message_text(
            f"✅ Today's {sched['departure_time']} departure now uses *{new_boat}*.",
            parse_mode="Markdown")
        # Notify customers
        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"🚤 *Schedule Update*\n\n"
                    f"Your booking `{bk['booking_ref']}` has a small update:\n\n"
                    f"The boat for your *{sched['departure_time']}* departure has been changed to *{new_boat}*.\n"
                    f"📌 Location: {sched.get('location','Jetty No. 1, Male')}\n\n"
                    f"All other details remain the same. Safe travels! 🌊",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Customer notify error: {e}")

    elif data.startswith("cancel_today_"):
        sched_id = int(data.split("_")[-1])
        today = datetime.now().date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO schedule_changes (schedule_id, change_date, note, status)
                VALUES ($1,$2,'Departure cancelled for today','cancelled')
                ON CONFLICT DO NOTHING
            """, sched_id, today)
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            bookings = await conn.fetch("""
                SELECT customer_telegram_id, booking_ref FROM bookings
                WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
            """, sched_id, today)
        await query.edit_message_text(f"❌ Today's {sched['departure_time']} departure marked as cancelled.")
        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"❌ *Departure Cancelled*\n\n"
                    f"We regret to inform you that your *{sched['departure_time']}* departure\n"
                    f"{sched['route_from']} → {sched['route_to']} has been cancelled today.\n\n"
                    f"Booking `{bk['booking_ref']}`\n\n"
                    f"Please contact the operator for rebooking or refund. Sorry for the inconvenience. 🙏",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Cancel notify error: {e}")

    # ── ADMIN PANEL CALLBACKS ──────────────────────────────────────────────────
    elif data == "adm_operators":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            ops = await conn.fetch("SELECT * FROM operators ORDER BY status, created_at DESC LIMIT 20")
        if not ops:
            await query.message.reply_text("No operators found.")
            return
        for op in ops:
            status_icon = {"pending":"⏳","approved":"✅","rejected":"❌"}.get(op["status"],"❓")
            rec = "🌟 " if op["is_recommended"] else ""
            msg = (
                f"{status_icon} {rec}*{op['business_name']}*\n"
                f"🛥️ {op['boat_name']} | 💺 {op['seat_count']} seats\n"
                f"👤 @{op['telegram_username'] or 'N/A'} (`{op['telegram_id']}`)\n"
                f"📞 {op['owner_contact'] or 'N/A'}\n"
                f"📅 {str(op['created_at'])[:10]}"
            )
            btns = []
            if op["status"] != "approved":
                btns.append([InlineKeyboardButton("✅ Approve", callback_data=f"approve_op_{op['id']}"),
                             InlineKeyboardButton("❌ Reject",  callback_data=f"reject_op_{op['id']}")])
            btns.append([
                InlineKeyboardButton("🌟 Recommend" if not op["is_recommended"] else "⭐ Un-recommend",
                    callback_data=f"admin_recommend_{op['id']}" if not op["is_recommended"] else f"admin_unrecommend_{op['id']}"),
                InlineKeyboardButton("🔄 Reset", callback_data=f"admin_reset_{op['id']}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_delete_{op['id']}")
            ])
            await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif data == "adm_bookings":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            bks = await conn.fetch("""
                SELECT b.*, o.business_name FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                ORDER BY b.created_at DESC LIMIT 15
            """)
        if not bks:
            await query.message.reply_text("No bookings yet.")
            return
        icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
        msg = "📦 *Recent Bookings:*\n\n"
        for b in bks:
            ic = icons.get(b["status"],"❓")
            msg += (f"{ic} `{b['booking_ref']}` — {b['business_name']}\n"
                   f"   👤 {b['customer_name'] or 'N/A'} | 📅 {b['travel_date']} | MVR {b['total_amount']}\n\n")
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "adm_revenue":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            today_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed' AND created_at::date=CURRENT_DATE")
            week_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed' AND created_at >= NOW()-INTERVAL '7 days'")
            month_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed' AND created_at >= NOW()-INTERVAL '30 days'")
            total_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed'")
            top_ops = await conn.fetch("""
                SELECT o.business_name, COUNT(*) as bookings, SUM(b.total_amount) as revenue
                FROM bookings b JOIN operators o ON b.operator_id=o.id
                WHERE b.status='confirmed'
                GROUP BY o.business_name ORDER BY revenue DESC LIMIT 5
            """)
        msg = (
            f"💰 *Revenue Report*\n\n"
            f"📅 Today: *MVR {today_rev:.2f}*\n"
            f"📅 This Week: *MVR {week_rev:.2f}*\n"
            f"📅 This Month: *MVR {month_rev:.2f}*\n"
            f"📈 All Time: *MVR {total_rev:.2f}*\n\n"
            f"🏆 *Top Operators:*\n"
        )
        for i, op in enumerate(top_ops, 1):
            msg += f"  {i}. {op['business_name']} — {op['bookings']} bookings | MVR {op['revenue']:.2f}\n"
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "adm_broadcast":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, ADMIN_AWAIT_BROADCAST, {})
        await query.message.reply_text(
            "📢 *Broadcast Message*\n\n"
            "Type the message to send to *all approved operators*:\n\n"
            "_Type_ `cancel` _to abort._",
            parse_mode="Markdown")

    elif data == "adm_upload_logo":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, ADMIN_AWAIT_LOGO, {})
        await query.message.reply_text(
            "🖼️ *Upload Samuga Travels Logo*\n\n"
            "Send the logo image now and it will appear on every ticket! 🎫",
            parse_mode="Markdown")

    elif data == "adm_settings":
        if not await admin_check(query, ctx): return
        samuga_logo = await get_setting("samuga_logo_url", "Not set")
        msg = (
            f"⚙️ *Settings*\n\n"
            f"🖼️ Samuga Logo URL:\n`{samuga_logo[:60]}...`\n\n"
            f"_More settings coming soon_"
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼️ Update Logo", callback_data="adm_upload_logo")],
                [InlineKeyboardButton("🔙 Back to Admin", callback_data="adm_back")]
            ]))

    elif data == "adm_schedules":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            scheds = await conn.fetch("""
                SELECT s.*, o.business_name FROM schedules s
                JOIN operators o ON s.operator_id=o.id
                WHERE s.is_active=TRUE ORDER BY o.business_name, s.departure_time
            """)
        if not scheds:
            await query.message.reply_text("No active schedules.")
            return
        msg = "🚤 *All Active Schedules:*\n\n"
        for s in scheds:
            msg += (f"🏢 *{s['business_name']}*\n"
                   f"  ⏰ {s['departure_time']} | {s['route_from']} → {s['route_to']}\n"
                   f"  📌 {s.get('location','N/A')} | 💺 {s['available_seats']} seats | MVR {s['price_per_seat']}\n\n")
        await query.message.reply_text(msg[:4000], parse_mode="Markdown")

    elif data == "adm_find_customer":
        if not await admin_check(query, ctx): return
        await query.message.reply_text(
            "🔍 Use: `/findcustomer <booking_ref or telegram_id>`\n\nExample: `/findcustomer ST-260629-0389`",
            parse_mode="Markdown")

    elif data == "adm_back":
        if not await admin_check(query, ctx): return
        await query.message.reply_text("Back to admin — type /admin", parse_mode="Markdown")

    elif data.startswith("urgent_review_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            op = await conn.fetchrow("SELECT * FROM operators WHERE id=$1", op_id)
        if op:
            urgency_msg = (
                f"🚨 *URGENT REVIEW REQUEST*\n\n"
                f"👤 @{op['telegram_username'] or op['telegram_id']} (`{op['telegram_id']}`)\n"
                f"🏢 *{op['business_name']}*\n"
                f"🛥️ {op['boat_name']}\n\n"
                f"Operator is requesting urgent approval."
            )
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Approve Now", callback_data=f"approve_op_{op_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_op_{op_id}")
            ]])
            try:
                await ctx.bot.send_message(ADMIN_GROUP_ID, urgency_msg,
                    parse_mode="Markdown", message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
                await query.answer("🚨 Urgent request sent to admin!", show_alert=True)
                await query.edit_message_text(
                    "🚨 *Urgent review request sent!*\n\n"
                    "Our team has been notified. You will hear back shortly.",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Urgent notify error: {e}")
                await query.answer("Failed to send. Try again.", show_alert=True)

    elif data.startswith("sched_boat_"):
        # Format: sched_boat_{boat_id}_{boat_name}
        parts_data = data.split("_", 3)
        boat_id = int(parts_data[2])
        boat_name_sel = parts_data[3] if len(parts_data) > 3 else "default"
        sd2 = await get_user_state(user.id)
        t2 = sd2.get("temp_data", {}) or {}
        import json as _j
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS sched_stops TEXT DEFAULT '[]'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS location TEXT DEFAULT 'Jetty No. 1, Male'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS run_days TEXT DEFAULT 'daily'")
            await conn.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS boat_name TEXT")
            await conn.execute("""
                INSERT INTO schedules (operator_id, route_from, route_to, departure_time,
                                       price_per_seat, total_seats, available_seats,
                                       sched_stops, location, run_days, boat_name)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, op["id"], t2.get("sched_from"), t2.get("sched_to"),
                t2.get("sched_time"), t2.get("sched_price"),
                t2.get("sched_seats",0), t2.get("sched_seats",0),
                _j.dumps(t2.get("sched_stops",[])),
                t2.get("sched_location","Jetty No. 1, Male"),
                t2.get("run_days","daily"),
                None if boat_name_sel == "default" else boat_name_sel)
        await set_user_state(user.id, OP_IDLE, {})
        boat_display = boat_name_sel if boat_name_sel != "default" else "Default"
        await query.edit_message_text(
            f"✅ *Schedule Added!*\n\n"
            f"📍 {t2.get('sched_from')} → {t2.get('sched_to')}\n"
            f"⏰ {t2.get('sched_time')} | 📅 {t2.get('run_days','daily')}\n"
            f"📌 {t2.get('sched_location','Jetty No. 1, Male')}\n"
            f"🚤 Boat: {boat_display}\n"
            f"💰 MVR {t2.get('sched_price')}/seat | 👥 {t2.get('sched_seats',0)} seats",
            parse_mode="Markdown")

    elif data == "op_fleet":
        op = await get_operator(user.id)
        if not op:
            await query.message.reply_text("⚠️ No operator profile.")
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 ORDER BY created_at", op["id"])
        if not boats:
            await query.message.reply_text(
                "🚤 *Your Fleet*\n\nNo boats added yet.\n\nAdd your first boat:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("➕ Add a Boat", callback_data="op_add_boat")
                ]]))
            return
        msg = "🚤 *Your Fleet:*\n\n"
        buttons = []
        for b in boats:
            status_icon = "✅" if b["status"] == "active" else "🔧"
            msg += f"{status_icon} *{b['boat_name']}* — {b['capacity']} seats\n"
            buttons.append([
                InlineKeyboardButton(f"🔧 Maintenance — {b['boat_name']}", callback_data=f"boat_maintenance_{b['id']}"),
                InlineKeyboardButton(f"✅ Active", callback_data=f"boat_active_{b['id']}")
            ])
        buttons.append([InlineKeyboardButton("➕ Add Another Boat", callback_data="op_add_boat")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data == "op_add_boat":
        await set_user_state(user.id, OP_AWAIT_BOAT_ADD_NAME, {})
        await query.message.reply_text(
            "🚤 *Add a Boat*\n\nWhat is this boat's name?\n\n_Example: SamugaTravels 1, Ocean Star_",
            parse_mode="Markdown")

    elif data.startswith("boat_maintenance_"):
        boat_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE boats SET status='maintenance' WHERE id=$1 RETURNING boat_name", boat_id)
        if row:
            await query.answer(f"🔧 {row['boat_name']} set to maintenance.", show_alert=True)
            await query.edit_message_text(f"🔧 *{row['boat_name']}* is now under maintenance.\nCustomers won't see it in available boats.", parse_mode="Markdown")

    elif data.startswith("boat_active_"):
        boat_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("UPDATE boats SET status='active' WHERE id=$1 RETURNING boat_name", boat_id)
        if row:
            await query.answer(f"✅ {row['boat_name']} is now active!", show_alert=True)
            await query.edit_message_text(f"✅ *{row['boat_name']}* is now active.", parse_mode="Markdown")

    elif data == "op_today":
        op = await get_operator(user.id)
        if not op:
            return
        from datetime import timedelta as _td
        today = datetime.now().date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            scheds = await conn.fetch("""
                SELECT s.*, COALESCE(sc.new_boat_name, s.boat_name) as active_boat,
                       COALESCE(sc.new_time, s.departure_time) as active_time,
                       sc.note as change_note
                FROM schedules s
                LEFT JOIN schedule_changes sc ON sc.schedule_id=s.id AND sc.change_date=$1
                WHERE s.operator_id=$2 AND s.is_active=TRUE
                ORDER BY s.departure_time
            """, today, op["id"])
            # Count bookings for each schedule today
            bookings_today = await conn.fetch("""
                SELECT schedule_id, COUNT(*) as count, SUM(passenger_count) as pax
                FROM bookings WHERE travel_date=$1 AND status='confirmed' AND operator_id=$2
                GROUP BY schedule_id
            """, today, op["id"])
        booking_map = {b["schedule_id"]: b for b in bookings_today}
        if not scheds:
            await query.message.reply_text("📅 No schedules found.")
            return
        msg = f"📅 *Today's Schedule — {today.strftime('%A, %d %b')}*\n\n"
        buttons = []
        for s in scheds:
            bk = booking_map.get(s["id"])
            pax = bk["pax"] if bk else 0
            count = bk["count"] if bk else 0
            change_note = f"\n⚠️ *Change:* {s['change_note']}" if s.get("change_note") else ""
            msg += (
                f"⏰ *{s['active_time']}* — {s['route_from']} → {s['route_to']}\n"
                f"🚤 {s['active_boat'] or 'Default boat'}\n"
                f"📌 {s.get('location','Jetty No. 1, Male')}\n"
                f"🎫 {count} bookings | 👥 {pax} passengers{change_note}\n\n"
            )
            buttons.append([InlineKeyboardButton(
                f"✏️ Change {s['active_time']} schedule",
                callback_data=f"change_sched_{s['id']}")])
        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("change_sched_"):
        sched_id = int(data.split("_")[-1])
        op = await get_operator(user.id)
        pool = await get_pool()
        async with pool.acquire() as conn:
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            boats = await conn.fetch("SELECT * FROM boats WHERE operator_id=$1 AND status='active'", op["id"])
        if not sched:
            await query.answer("Schedule not found.", show_alert=True)
            return
        buttons = []
        for b in boats:
            buttons.append([InlineKeyboardButton(
                f"🚤 Swap to {b['boat_name']}",
                callback_data=f"swap_boat_{sched_id}_{b['boat_name']}")])
        buttons.append([InlineKeyboardButton("⏰ Change Time", callback_data=f"swap_time_{sched_id}")])
        buttons.append([InlineKeyboardButton("❌ Cancel Today's Departure", callback_data=f"cancel_today_{sched_id}")])
        await query.message.reply_text(
            f"✏️ *Change Today's Schedule*\n\n"
            f"⏰ {sched['departure_time']} — {sched['route_from']} → {sched['route_to']}\n"
            f"📌 {sched.get('location','Jetty No. 1, Male')}\n\n"
            f"What would you like to change?",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))

    elif data.startswith("swap_boat_"):
        parts_s = data.split("_", 3)
        sched_id = int(parts_s[2])
        new_boat = parts_s[3]
        today = datetime.now().date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO schedule_changes (schedule_id, change_date, new_boat_name, note)
                VALUES ($1,$2,$3,'Boat swapped by operator')
                ON CONFLICT DO NOTHING
            """, sched_id, today, new_boat)
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            # Notify confirmed customers for today
            bookings = await conn.fetch("""
                SELECT customer_telegram_id, booking_ref FROM bookings
                WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
            """, sched_id, today)
        await query.edit_message_text(
            f"✅ Today's {sched['departure_time']} departure now uses *{new_boat}*.",
            parse_mode="Markdown")
        # Notify customers
        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"🚤 *Schedule Update*\n\n"
                    f"Your booking `{bk['booking_ref']}` has a small update:\n\n"
                    f"The boat for your *{sched['departure_time']}* departure has been changed to *{new_boat}*.\n"
                    f"📌 Location: {sched.get('location','Jetty No. 1, Male')}\n\n"
                    f"All other details remain the same. Safe travels! 🌊",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Customer notify error: {e}")

    elif data.startswith("cancel_today_"):
        sched_id = int(data.split("_")[-1])
        today = datetime.now().date()
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO schedule_changes (schedule_id, change_date, note, status)
                VALUES ($1,$2,'Departure cancelled for today','cancelled')
                ON CONFLICT DO NOTHING
            """, sched_id, today)
            sched = await conn.fetchrow("SELECT * FROM schedules WHERE id=$1", sched_id)
            bookings = await conn.fetch("""
                SELECT customer_telegram_id, booking_ref FROM bookings
                WHERE schedule_id=$1 AND travel_date=$2 AND status='confirmed'
            """, sched_id, today)
        await query.edit_message_text(f"❌ Today's {sched['departure_time']} departure marked as cancelled.")
        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"❌ *Departure Cancelled*\n\n"
                    f"We regret to inform you that your *{sched['departure_time']}* departure\n"
                    f"{sched['route_from']} → {sched['route_to']} has been cancelled today.\n\n"
                    f"Booking `{bk['booking_ref']}`\n\n"
                    f"Please contact the operator for rebooking or refund. Sorry for the inconvenience. 🙏",
                    parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Cancel notify error: {e}")

    # ── ADMIN PANEL CALLBACKS ──────────────────────────────────────────────────
    elif data == "adm_operators":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            ops = await conn.fetch("SELECT * FROM operators ORDER BY status, created_at DESC LIMIT 20")
        if not ops:
            await query.message.reply_text("No operators found.")
            return
        for op in ops:
            status_icon = {"pending":"⏳","approved":"✅","rejected":"❌"}.get(op["status"],"❓")
            rec = "🌟 " if op["is_recommended"] else ""
            msg = (
                f"{status_icon} {rec}*{op['business_name']}*\n"
                f"🛥️ {op['boat_name']} | 💺 {op['seat_count']} seats\n"
                f"👤 @{op['telegram_username'] or 'N/A'} (`{op['telegram_id']}`)\n"
                f"📞 {op['owner_contact'] or 'N/A'}\n"
                f"📅 {str(op['created_at'])[:10]}"
            )
            btns = []
            if op["status"] != "approved":
                btns.append([InlineKeyboardButton("✅ Approve", callback_data=f"approve_op_{op['id']}"),
                             InlineKeyboardButton("❌ Reject",  callback_data=f"reject_op_{op['id']}")])
            btns.append([
                InlineKeyboardButton("🌟 Recommend" if not op["is_recommended"] else "⭐ Un-recommend",
                    callback_data=f"admin_recommend_{op['id']}" if not op["is_recommended"] else f"admin_unrecommend_{op['id']}"),
                InlineKeyboardButton("🔄 Reset", callback_data=f"admin_reset_{op['id']}"),
                InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_delete_{op['id']}")
            ])
            await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(btns))

    elif data == "adm_bookings":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            bks = await conn.fetch("""
                SELECT b.*, o.business_name FROM bookings b
                JOIN operators o ON b.operator_id=o.id
                ORDER BY b.created_at DESC LIMIT 15
            """)
        if not bks:
            await query.message.reply_text("No bookings yet.")
            return
        icons = {"pending_payment":"⏳","pending_confirmation":"🔄","confirmed":"✅","cancelled":"❌"}
        msg = "📦 *Recent Bookings:*\n\n"
        for b in bks:
            ic = icons.get(b["status"],"❓")
            msg += (f"{ic} `{b['booking_ref']}` — {b['business_name']}\n"
                   f"   👤 {b['customer_name'] or 'N/A'} | 📅 {b['travel_date']} | MVR {b['total_amount']}\n\n")
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "adm_revenue":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            today_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed' AND created_at::date=CURRENT_DATE")
            week_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed' AND created_at >= NOW()-INTERVAL '7 days'")
            month_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed' AND created_at >= NOW()-INTERVAL '30 days'")
            total_rev = await conn.fetchval(
                "SELECT COALESCE(SUM(total_amount),0) FROM bookings WHERE status='confirmed'")
            top_ops = await conn.fetch("""
                SELECT o.business_name, COUNT(*) as bookings, SUM(b.total_amount) as revenue
                FROM bookings b JOIN operators o ON b.operator_id=o.id
                WHERE b.status='confirmed'
                GROUP BY o.business_name ORDER BY revenue DESC LIMIT 5
            """)
        msg = (
            f"💰 *Revenue Report*\n\n"
            f"📅 Today: *MVR {today_rev:.2f}*\n"
            f"📅 This Week: *MVR {week_rev:.2f}*\n"
            f"📅 This Month: *MVR {month_rev:.2f}*\n"
            f"📈 All Time: *MVR {total_rev:.2f}*\n\n"
            f"🏆 *Top Operators:*\n"
        )
        for i, op in enumerate(top_ops, 1):
            msg += f"  {i}. {op['business_name']} — {op['bookings']} bookings | MVR {op['revenue']:.2f}\n"
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data == "adm_broadcast":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, ADMIN_AWAIT_BROADCAST, {})
        await query.message.reply_text(
            "📢 *Broadcast Message*\n\n"
            "Type the message to send to *all approved operators*:\n\n"
            "_Type_ `cancel` _to abort._",
            parse_mode="Markdown")

    elif data == "adm_upload_logo":
        if not await admin_check(query, ctx): return
        await set_user_state(user.id, ADMIN_AWAIT_LOGO, {})
        await query.message.reply_text(
            "🖼️ *Upload Samuga Travels Logo*\n\n"
            "Send the logo image now and it will appear on every ticket! 🎫",
            parse_mode="Markdown")

    elif data == "adm_settings":
        if not await admin_check(query, ctx): return
        samuga_logo = await get_setting("samuga_logo_url", "Not set")
        msg = (
            f"⚙️ *Settings*\n\n"
            f"🖼️ Samuga Logo URL:\n`{samuga_logo[:60]}...`\n\n"
            f"_More settings coming soon_"
        )
        await query.message.reply_text(msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼️ Update Logo", callback_data="adm_upload_logo")],
                [InlineKeyboardButton("🔙 Back to Admin", callback_data="adm_back")]
            ]))

    elif data == "adm_schedules":
        if not await admin_check(query, ctx): return
        pool = await get_pool()
        async with pool.acquire() as conn:
            scheds = await conn.fetch("""
                SELECT s.*, o.business_name FROM schedules s
                JOIN operators o ON s.operator_id=o.id
                WHERE s.is_active=TRUE ORDER BY o.business_name, s.departure_time
            """)
        if not scheds:
            await query.message.reply_text("No active schedules.")
            return
        msg = "🚤 *All Active Schedules:*\n\n"
        for s in scheds:
            msg += (f"🏢 *{s['business_name']}*\n"
                   f"  ⏰ {s['departure_time']} | {s['route_from']} → {s['route_to']}\n"
                   f"  📌 {s.get('location','N/A')} | 💺 {s['available_seats']} seats | MVR {s['price_per_seat']}\n\n")
        await query.message.reply_text(msg[:4000], parse_mode="Markdown")

    elif data == "adm_find_customer":
        if not await admin_check(query, ctx): return
        await query.message.reply_text(
            "🔍 Use: `/findcustomer <booking_ref or telegram_id>`\n\nExample: `/findcustomer ST-260629-0389`",
            parse_mode="Markdown")

    elif data == "adm_back":
        if not await admin_check(query, ctx): return
        await query.message.reply_text("Back to admin — type /admin", parse_mode="Markdown")

    elif data.startswith("urgent_review_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            op = await conn.fetchrow("SELECT * FROM operators WHERE id=$1", op_id)
        if not op:
            await query.answer("Application not found.", show_alert=True)
            return
        urgent_msg = (
            f"🚨 *URGENT REVIEW REQUEST*\n\n"
            f"👤 @{op['telegram_username'] or 'N/A'} (`{op['telegram_id']}`)\n"
            f"🏢 *{op['business_name']}*\n"
            f"🛥️ {op['boat_name']}\n\n"
            f"⚡ Operator is requesting urgent approval."
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve Now", callback_data=f"approve_op_{op_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_op_{op_id}")
        ]])
        try:
            await ctx.bot.send_message(ADMIN_GROUP_ID, urgent_msg, parse_mode="Markdown",
                                       message_thread_id=ADMIN_THREAD_ID, reply_markup=kb)
            await query.edit_message_text(
                "🚨 *Urgent request sent to admin!*\n\n"
                "Our team has been notified and will review your application as soon as possible. 🙏",
                parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Urgent cb error: {e}")
            await query.answer("Could not send. Contact @SamugaTravels.", show_alert=True)

    elif data.startswith("admin_delete_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("DELETE FROM operators WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            # Also reset user state so they can re-register
            await set_user_state(row["telegram_id"], CX_IDLE, {}, role="customer")
            await query.edit_message_text(f"🗑️ Operator *{row['business_name']}* deleted. They can now re-register.", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(row["telegram_id"],
                    "ℹ️ Your operator profile has been removed by Samuga Travels admin.\n"
                    "You may register again with /register.", parse_mode="Markdown")
            except: pass

    elif data.startswith("admin_reset_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET status='pending' WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            await set_user_state(row["telegram_id"], CX_IDLE, {}, role="customer")
            await query.edit_message_text(f"🔄 Operator *{row['business_name']}* reset to pending.", parse_mode="Markdown")

    elif data.startswith("admin_recommend_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET is_recommended=TRUE WHERE id=$1 RETURNING telegram_id, business_name", op_id)
        if row:
            await query.edit_message_text(f"🌟 *{row['business_name']}* is now Recommended!", parse_mode="Markdown")
            try:
                await ctx.bot.send_message(row["telegram_id"],
                    "🌟 *Congratulations!* Your business is now *Recommended by Samuga Travels!*\n\n"
                    "Customers will see your badge when browsing boats. 🎉", parse_mode="Markdown")
            except: pass

    elif data.startswith("admin_unrecommend_"):
        op_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE operators SET is_recommended=FALSE, review_text=NULL WHERE id=$1 RETURNING business_name", op_id)
        if row:
            await query.edit_message_text(f"⭐ Recommended badge removed from *{row['business_name']}*.", parse_mode="Markdown")

    elif data.startswith("not_received_"):
        booking_id = int(data.split("_")[-1])
        pool = await get_pool()
        async with pool.acquire() as conn:
            bk = await conn.fetchrow("SELECT * FROM bookings WHERE id=$1", booking_id)
        if not bk:
            await query.answer("Booking not found.", show_alert=True)
            return
        # Notify customer
        try:
            await ctx.bot.send_message(bk["customer_telegram_id"],
                f"⚠️ *Payment Not Confirmed*\n\n"
                f"Hi there! The operator could not verify your payment for booking `{bk['booking_ref']}`.\n\n"
                f"This could be because:\n"
                f"• The transfer was sent to the wrong account\n"
                f"• The amount was incorrect\n"
                f"• The screenshot was unclear\n\n"
                f"Please double-check and resend your payment slip, or contact the operator directly. 🙏",
                parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Not received notify error: {e}")
        await query.edit_message_caption(
            caption=f"❌ Customer notified — payment not confirmed for `{bk['booking_ref']}`.",
            parse_mode="Markdown")

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
        # Fetch the saved operator id
        row = await conn.fetchrow("SELECT id FROM operators WHERE telegram_id=$1", user.id)
        return row["id"] if row else 0

async def notify_admin_new_op(ctx, user, temp: dict, op_id: int = 0):
    if op_id == 0:
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
    # Get operator telegram ID from sel dict or flat temp keys or DB
    op_tg_id = sel.get("op_telegram_id") or temp.get("sel_op_tg")
    if not op_tg_id:
        pool = await get_pool()
        async with pool.acquire() as conn:
            op_id = sel.get("operator_id") or temp.get("sel_operator_id")
            row = await conn.fetchrow("SELECT telegram_id FROM operators WHERE id=$1", op_id)
        if not row:
            logger.error(f"Could not find operator for booking {booking_id}")
            return
        op_tg_id = row["telegram_id"]

    pax = temp.get("passengers_collected",[])
    pax_lines = "\n".join([f"  {i+1}. {p['name']} ({p['id_number']})" for i,p in enumerate(pax)])
    dep_time = sel.get("departure_time") or temp.get("sel_time","")
    msg = (
        f"💳 *New Payment Received!*\n\n"
        f"🔖 Ref: `{ref}`\n"
        f"👤 *Customer:* {temp.get('cx_name','N/A')} | 📞 {temp.get('cx_phone','N/A')}\n"
        f"📍 {temp.get('route_from')} → {temp.get('route_to')}\n"
        f"📅 {temp.get('travel_date')} @ {dep_time}\n"
        f"👥 {temp.get('passenger_count')} passengers:\n{pax_lines}\n"
        f"💰 MVR {temp.get('total_amount')}\n\n"
        f"Review the slip and confirm below 👇"
    )
    try:
        await ctx.bot.send_photo(op_tg_id, photo=slip_file_id, caption=msg, parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Confirm & Send Ticket", callback_data=f"confirm_booking_{booking_id}")],
                [InlineKeyboardButton("❌ Not Received / Wrong Transfer", callback_data=f"not_received_{booking_id}")]
            ]))
        logger.info(f"✅ Operator {op_tg_id} notified for booking {booking_id}")
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
        try: booking_dict["passengers"] = json.loads(passengers)
        except: booking_dict["passengers"] = []

    # Fetch full operator info including contact
    pool2 = await get_pool()
    async with pool2.acquire() as conn2:
        full_op = await conn2.fetchrow("SELECT * FROM operators WHERE id=$1", booking["operator_id"])
        sched_full = await conn2.fetchrow("SELECT * FROM schedules WHERE id=$1", booking["schedule_id"])

    op_dict = {
        "business_name": booking["business_name"],
        "boat_name": booking["boat_name"],
        "logo_url": booking["logo_url"],
        "owner_contact": full_op["owner_contact"] if full_op else "",
        "telegram_id": full_op["telegram_id"] if full_op else 0,
    }
    sched_dict = {
        "route_from": booking["route_from"],
        "route_to": booking["route_to"],
        "departure_time": booking["departure_time"],
        "price_per_seat": booking["price_per_seat"],
        "location": sched_full["location"] if sched_full and "location" in sched_full.keys() else "Jetty No. 1, Male",
    }

    pdf_bytes = await generate_ticket_pdf(booking_dict, op_dict, sched_dict)
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
        ),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 Book Your Next Trip", callback_data="cx_search")],
            [InlineKeyboardButton("📋 My Bookings", callback_data="cx_my_bookings")]
        ]))
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

# ── SCHEDULED JOBS ───────────────────────────────────────────────────────────
async def job_morning_ping(ctx: ContextTypes.DEFAULT_TYPE):
    """6:00 AM MVT — ping all operators about today's schedules"""
    today = datetime.now().date()
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Get all approved operators with schedules today
        operators = await conn.fetch("""
            SELECT DISTINCT o.telegram_id, o.business_name, o.id as op_id
            FROM operators o
            JOIN schedules s ON s.operator_id = o.id
            WHERE o.status = 'approved' AND s.is_active = TRUE
        """)
        for op in operators:
            scheds = await conn.fetch("""
                SELECT * FROM schedules WHERE operator_id=$1 AND is_active=TRUE
                ORDER BY departure_time
            """, op["op_id"])
            if not scheds:
                continue
            sched_lines = "\n".join([
                f"  ⏰ {s['departure_time']} — {s['route_from']} → {s['route_to']} | 📌 {s.get('location','Jetty No. 1, Male')}"
                for s in scheds
            ])
            buttons = [[InlineKeyboardButton("📅 View & Manage Today", callback_data="op_today")]]
            try:
                await ctx.bot.send_message(op["telegram_id"],
                    f"🌅 *Good morning, {op['business_name']}!*\n\n"
                    f"Today's schedules:\n{sched_lines}\n\n"
                    f"⚠️ Any changes? Tap below to swap boats, update times, or cancel a departure.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Morning ping failed for {op['telegram_id']}: {e}")

async def job_departure_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """Run every 5 minutes — send 45-min reminders to confirmed customers"""
    from datetime import timedelta
    now = datetime.now()
    today = now.date()
    # Target: departures happening in 40-50 minutes from now
    remind_from = (now.replace(second=0, microsecond=0) + timedelta(minutes=40)).strftime("%H:%M")
    remind_to   = (now.replace(second=0, microsecond=0) + timedelta(minutes=50)).strftime("%H:%M")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Find bookings with departures in ~45 min, not yet reminded
        bookings = await conn.fetch("""
            SELECT b.customer_telegram_id, b.booking_ref, b.passenger_count,
                   COALESCE(sc.new_time, s.departure_time) as dep_time,
                   COALESCE(sc.new_boat_name, s.boat_name, o.boat_name) as boat_name,
                   s.location, s.route_from, s.route_to, o.business_name
            FROM bookings b
            JOIN schedules s ON b.schedule_id = s.id
            JOIN operators o ON b.operator_id = o.id
            LEFT JOIN schedule_changes sc ON sc.schedule_id=s.id AND sc.change_date=$1 AND sc.status='active'
            WHERE b.travel_date = $1
              AND b.status = 'confirmed'
              AND b.reminder_sent = FALSE
              AND COALESCE(sc.new_time, s.departure_time) >= $2
              AND COALESCE(sc.new_time, s.departure_time) <= $3
        """, today, remind_from, remind_to)

        for bk in bookings:
            try:
                await ctx.bot.send_message(bk["customer_telegram_id"],
                    f"🌊 *Almost time to set sail!*\n\n"
                    f"Hey there! Just a friendly reminder that your boat departs in about *45 minutes*. "
                    f"Please make your way to the jetty soon! 😊\n\n"
                    f"🚤 *{bk['boat_name'] or bk['business_name']}*\n"
                    f"📍 *{bk['route_from']} → {bk['route_to']}*\n"
                    f"⏰ Departure: *{bk['dep_time']}*\n"
                    f"📌 Location: *{bk.get('location') or 'Jetty No. 1, Male'}*\n"
                    f"🎫 Booking: `{bk['booking_ref']}` | 👥 {bk['passenger_count']} pax\n\n"
                    f"📱 You can use the *FollowMe* app to track your boat in real time.\n\n"
                    f"Wishing you a safe, smooth and wonderful journey! 🌟\n"
                    f"Safe travels from all of us at *Samuga Travels* 🌊🤝",
                    parse_mode="Markdown")
                # Mark as reminded
                await conn.execute(
                    "UPDATE bookings SET reminder_sent=TRUE WHERE booking_ref=$1",
                    bk["booking_ref"])
                logger.info(f"✅ Reminder sent to {bk['customer_telegram_id']} for {bk['booking_ref']}")
            except Exception as e:
                logger.error(f"Reminder failed for {bk['customer_telegram_id']}: {e}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
async def main():
    # Init DB first before anything else
    logger.info("🌊 Starting Samuga Travels Bot v1.2...")
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
    app.add_handler(CommandHandler("admin",     cmd_admin))
    app.add_handler(CommandHandler("ops",       cmd_ops))
    app.add_handler(CommandHandler("urgent",       cmd_urgent))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("findcustomer", cmd_findcustomer))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    # ── Scheduled jobs ──
    from datetime import time as dt_time
    jq = app.job_queue
    # Morning ping: 6:00 AM MVT = 01:00 UTC
    jq.run_daily(job_morning_ping, time=dt_time(1, 0, 0), name="morning_ping")
    # Departure reminders: every 5 minutes
    jq.run_repeating(job_departure_reminders, interval=300, first=30, name="departure_reminders")
    logger.info("✅ Scheduled jobs registered")

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    logger.info("🌊 Samuga Travels Bot v1.2 LIVE!")

    # Graceful shutdown on SIGTERM (Railway stop signal)
    stop_event = asyncio.Event()
    def _handle_sigterm(*_):
        logger.info("🛑 SIGTERM received — shutting down gracefully...")
        stop_event.set()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    await stop_event.wait()
    logger.info("👋 Stopping bot...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())

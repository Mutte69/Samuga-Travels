"""Boat request marketplace for Samuga Travels.

Handles the no-scheduled-boat flow:
customer request -> admin topic -> ping operators -> operator offers -> admin assign -> invoice/payment flow.
Kept separate from bot.py so the main bot stays easier to maintain.
"""

import os
import re
import json
import logging
from datetime import datetime, date
from decimal import Decimal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

BOAT_REQUEST_THREAD_ID = int(os.environ.get("BOAT_REQUEST_THREAD_ID", "70"))
BOT_USERNAME = os.environ.get("BOT_USERNAME", "SamugaTravelsBot").lstrip("@")

# States stored in user_states.state
BR_AWAIT_TIME = "br_await_time"
BR_AWAIT_CONTACT = "br_await_contact"
BR_AWAIT_PAX = "br_await_pax"
BR_AWAIT_TRIP_TYPE = "br_await_trip_type"
BR_AWAIT_NOTES = "br_await_notes"
OP_AWAIT_BR_OFFER_PRICE = "op_await_br_offer_price"
ADMIN_AWAIT_BR_MANUAL_PRICE = "admin_await_br_manual_price"
ADMIN_AWAIT_BR_MANUAL_PAYMODE = "admin_await_br_manual_paymode"
ADMIN_AWAIT_BR_OFFER_CUSTOMER_PRICE = "admin_await_br_offer_customer_price"
ADMIN_AWAIT_BR_MANAGED_OPERATOR_COST = "admin_await_br_managed_operator_cost"


def _money(v) -> str:
    try:
        return f"{float(v):,.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v or "0")


def _clean(txt: str) -> str:
    return " ".join(str(txt or "").strip().split())


def _parse_price(text: str):
    txt = (text or "").upper().replace(",", "")
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(MVR|RF|MRF|USD|DOLLAR|DOLLARS)?", txt)
    if not m:
        return None, "MVR"
    cur = (m.group(2) or "MVR").upper()
    if cur in ("RF", "MRF"):
        cur = "MVR"
    if cur in ("DOLLAR", "DOLLARS"):
        cur = "USD"
    return Decimal(m.group(1)), cur


def _parse_int(text: str):
    m = re.search(r"\d+", str(text or ""))
    return int(m.group()) if m else None


def _ref() -> str:
    return "BR-" + datetime.utcnow().strftime("%y%m%d-%H%M%S")


def _main_menu_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])


def request_boat_button():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚤 Request a Boat", callback_data="br_start_request")],
        [InlineKeyboardButton("🔍 Search Again", callback_data="cx_search")],
    ])


async def init_boat_request_db(get_pool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS boat_requests (
                id SERIAL PRIMARY KEY,
                request_ref TEXT UNIQUE NOT NULL,
                customer_telegram_id BIGINT,
                customer_name TEXT,
                customer_username TEXT,
                customer_contact TEXT,
                route_from TEXT NOT NULL,
                route_to TEXT NOT NULL,
                travel_date DATE NOT NULL,
                preferred_time TEXT,
                passenger_count INTEGER DEFAULT 1,
                trip_type TEXT DEFAULT 'oneway',
                return_route TEXT,
                pickup_location TEXT,
                notes TEXT,
                status TEXT DEFAULT 'open',
                assigned_operator_id INTEGER REFERENCES operators(id) ON DELETE SET NULL,
                accepted_offer_id INTEGER,
                created_by TEXT DEFAULT 'customer',
                invoice_booking_id INTEGER,
                admin_message_id BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                closed_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS boat_request_offers (
                id SERIAL PRIMARY KEY,
                request_id INTEGER REFERENCES boat_requests(id) ON DELETE CASCADE,
                operator_id INTEGER REFERENCES operators(id) ON DELETE SET NULL,
                price NUMERIC(12,2) NOT NULL,
                currency TEXT DEFAULT 'MVR',
                operator_note TEXT,
                customer_price NUMERIC(12,2),
                operator_cost NUMERIC(12,2),
                payment_mode TEXT DEFAULT 'operator_direct',
                samuga_margin NUMERIC(12,2) DEFAULT 0,
                status TEXT DEFAULT 'pending_admin',
                created_at TIMESTAMP DEFAULT NOW(),
                accepted_at TIMESTAMP,
                rejected_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS boat_request_events (
                id SERIAL PRIMARY KEY,
                request_id INTEGER REFERENCES boat_requests(id) ON DELETE CASCADE,
                actor_type TEXT,
                actor_telegram_id BIGINT,
                event_type TEXT,
                event_note TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE boat_request_offers ADD COLUMN IF NOT EXISTS customer_price NUMERIC(12,2)")
        await conn.execute("ALTER TABLE boat_request_offers ADD COLUMN IF NOT EXISTS operator_cost NUMERIC(12,2)")
        await conn.execute("ALTER TABLE boat_request_offers ADD COLUMN IF NOT EXISTS payment_mode TEXT DEFAULT 'operator_direct'")
        await conn.execute("ALTER TABLE boat_request_offers ADD COLUMN IF NOT EXISTS samuga_margin NUMERIC(12,2) DEFAULT 0")
        await conn.execute("ALTER TABLE boat_requests ADD COLUMN IF NOT EXISTS payment_mode TEXT DEFAULT 'operator_direct'")
        await conn.execute("ALTER TABLE boat_requests ADD COLUMN IF NOT EXISTS customer_price NUMERIC(12,2)")
        await conn.execute("ALTER TABLE boat_requests ADD COLUMN IF NOT EXISTS operator_cost NUMERIC(12,2)")
        await conn.execute("ALTER TABLE boat_requests ADD COLUMN IF NOT EXISTS samuga_margin NUMERIC(12,2) DEFAULT 0")
        await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_br_offer_one_per_operator ON boat_request_offers(request_id, operator_id)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_boat_requests_status ON boat_requests(status)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_boat_requests_customer ON boat_requests(customer_telegram_id)")
    logger.info("✅ Boat request marketplace DB ready")


async def _event(conn, request_id: int, actor_type: str, actor_tg: int | None, event_type: str, note: str = ""):
    await conn.execute("""
        INSERT INTO boat_request_events (request_id, actor_type, actor_telegram_id, event_type, event_note)
        VALUES ($1,$2,$3,$4,$5)
    """, request_id, actor_type, actor_tg, event_type, note[:1000])


def _admin_request_text(row) -> str:
    return (
        f"🚤 New Boat Request\n\n"
        f"Ref: {row['request_ref']}\n"
        f"Customer: {row.get('customer_name') or 'Customer'}\n"
        f"Telegram: {row.get('customer_telegram_id') or '-'}\n"
        f"Contact: {row.get('customer_contact') or '-'}\n\n"
        f"Route: {row['route_from']} → {row['route_to']}\n"
        f"Date: {row['travel_date']} @ {row.get('preferred_time') or 'Flexible'}\n"
        f"Passengers: {row.get('passenger_count') or 1}\n"
        f"Trip type: {row.get('trip_type') or 'oneway'}\n"
        f"Notes: {row.get('notes') or '-'}\n\n"
        f"Status: {row.get('status') or 'open'}"
    )


def _admin_request_kb(request_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Ping Operators", callback_data=f"br_ping_{request_id}")],
        [InlineKeyboardButton("✅ Assign Operator", callback_data=f"br_manual_assign_{request_id}")],
        [InlineKeyboardButton("❌ Close Request", callback_data=f"br_close_{request_id}")],
    ])


def _operator_ping_text(row) -> str:
    return (
        f"🚤 Boat Request Available\n\n"
        f"Route: {row['route_from']} → {row['route_to']}\n"
        f"Date: {row['travel_date']} @ {row.get('preferred_time') or 'Flexible'}\n"
        f"Passengers: {row.get('passenger_count') or 1}\n"
        f"Trip type: {row.get('trip_type') or 'oneway'}\n\n"
        f"Can you take this trip?"
    )


def _operator_ping_kb(request_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Take Trip", callback_data=f"br_take_{request_id}")],
        [InlineKeyboardButton("❌ Not Available", callback_data=f"br_decline_{request_id}")],
    ])


def _payment_lines(op: dict) -> str:
    lines = []
    try:
        accounts = json.loads(op.get("payment_accounts") or "[]")
        for acc in accounts:
            bank = _clean(acc.get("bank") or "Bank").upper()
            num = _clean(acc.get("number") or acc.get("account") or "")
            name = _clean(acc.get("name") or acc.get("account_name") or op.get("business_name") or "")
            if num:
                lines.append(f"{bank}: {num} — {name}")
    except Exception:
        pass
    bml = _clean(op.get("bml_account") or "")
    if bml and not any(bml in x for x in lines):
        lines.insert(0, f"BML: {bml} — {op.get('business_name') or 'Operator'}")
    if not lines:
        lines.append("Payment account: Please check with Samuga Travels / operator")
    return "\n".join(lines)


async def _samuga_payment_lines(pool) -> str:
    """Use Samuga Travels payment accounts saved in Admin > Settings > Subscriptions."""
    try:
        async with pool.acquire() as conn:
            raw = await conn.fetchval("SELECT value FROM settings WHERE key='subscription_accounts'")
        accounts = json.loads(raw or "[]")
        lines = []
        for acc in accounts:
            bank = _clean(acc.get("bank") or "Bank").upper()
            num = _clean(acc.get("number") or "")
            name = _clean(acc.get("name") or "Samuga Travels")
            if num:
                lines.append(f"{bank}: {num} — {name}")
        if lines:
            return "\n".join(lines)
    except Exception as e:
        logger.error(f"Samuga payment accounts read failed: {e}")
    return "Samuga Travels payment account is not set yet. Please contact Samuga Travels admin."


def _payment_mode_kb(offer_id: int, allow_edit: bool = False):
    rows = [
        [InlineKeyboardButton("🏦 Operator Direct Payment", callback_data=f"br_pay_direct_{offer_id}")],
        [InlineKeyboardButton("💼 Samuga Managed Payment", callback_data=f"br_pay_managed_{offer_id}")],
    ]
    if allow_edit:
        rows.insert(1, [InlineKeyboardButton("💼 Managed + Edit Customer Price", callback_data=f"br_pay_managed_edit_{offer_id}")])
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
    return InlineKeyboardMarkup(rows)


async def _submit_customer_request(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict):
    user = update.effective_user
    sd = await deps["get_user_state"](user.id)
    t = sd.get("temp_data", {}) or {}
    route_from = _clean(t.get("route_from"))
    route_to = _clean(t.get("route_to"))
    travel_date_raw = t.get("travel_date")
    if not route_from or not route_to or not travel_date_raw:
        await update.message.reply_text("⚠️ Request details expired. Please search the route again.", reply_markup=_main_menu_kb())
        await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
        return True
    try:
        travel_date = date.fromisoformat(str(travel_date_raw))
    except Exception:
        travel_date = datetime.strptime(str(travel_date_raw), "%d-%m-%Y").date()
    pool = await deps["get_pool"]()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO boat_requests
              (request_ref, customer_telegram_id, customer_name, customer_username, customer_contact,
               route_from, route_to, travel_date, preferred_time, passenger_count, trip_type, notes, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,'open')
            RETURNING *
        """, _ref(), user.id, user.full_name, user.username or "", t.get("customer_contact"),
            route_from, route_to, travel_date, t.get("preferred_time") or "Flexible",
            int(t.get("passenger_count") or 1), t.get("trip_type") or "oneway", t.get("notes") or "")
        await _event(conn, row["id"], "customer", user.id, "request_created", "Customer requested a boat")
    # Send to admin topic
    try:
        sent = await ctx.bot.send_message(
            deps["ADMIN_GROUP_ID"],
            _admin_request_text(dict(row)),
            message_thread_id=BOAT_REQUEST_THREAD_ID,
            reply_markup=_admin_request_kb(row["id"])
        )
        async with pool.acquire() as conn:
            await conn.execute("UPDATE boat_requests SET admin_message_id=$1 WHERE id=$2", sent.message_id, row["id"])
    except Exception as e:
        logger.error(f"Boat request admin notify failed: {e}", exc_info=True)
        try:
            await ctx.bot.send_message(deps["ADMIN_GROUP_ID"], f"🚨 Boat request admin-topic send failed: {e}")
        except Exception:
            pass
    await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
    await update.message.reply_text(
        "✅ Boat request sent!\n\nSamuga Travels will check with available operators and contact you when an offer is available.",
        reply_markup=_main_menu_kb())
    return True



async def _assign_offer_and_create_invoice(ctx: ContextTypes.DEFAULT_TYPE, deps: dict, admin_user_id: int, offer_id: int, reply_message, payment_mode: str = "operator_direct", customer_price=None):
    """Assign an offer, create invoice/booking, and notify customer/operator.
    Used by both operator-submitted offers and admin manual assignments.
    """
    pool = await deps["get_pool"]()
    async with pool.acquire() as conn:
        offer = await conn.fetchrow("""
            SELECT bo.*, o.business_name, o.telegram_id AS op_tg
            FROM boat_request_offers bo
            JOIN operators o ON bo.operator_id=o.id
            WHERE bo.id=$1
        """, offer_id)
        if not offer:
            await reply_message.reply_text("⚠️ Offer not found.")
            return True
        req = await conn.fetchrow("SELECT * FROM boat_requests WHERE id=$1", offer["request_id"])
        if not req or req["status"] in ("assigned", "closed", "cancelled"):
            await reply_message.reply_text("⚠️ This request is already closed or assigned.")
            return True
        op_row = await conn.fetchrow("SELECT * FROM operators WHERE id=$1", offer["operator_id"])
    req = dict(req)
    offer = dict(offer)
    op_row = dict(op_row)
    payment_mode = payment_mode if payment_mode in ("operator_direct", "samuga_managed") else "operator_direct"
    operator_cost = Decimal(str(offer.get("price") or 0))
    customer_price_dec = Decimal(str(customer_price if customer_price is not None else offer.get("customer_price") or offer.get("price") or 0))
    samuga_margin = customer_price_dec - operator_cost if payment_mode == "samuga_managed" else Decimal("0")
    parsed = {
        "customer_name": req.get("customer_name") or "Customer",
        "route_from": req["route_from"],
        "route_to": req["route_to"],
        "travel_date": str(req["travel_date"]),
        "departure_time": req.get("preferred_time") or "Flexible",
        "total_amount": float(customer_price_dec),
        "currency": offer.get("currency") or "MVR",
        "passenger_count": int(req.get("passenger_count") or 1),
        "trip_type": req.get("trip_type") or "oneway",
        "return_to": req.get("return_route") or None,
    }
    location = req.get("pickup_location") or req["route_from"]
    try:
        booking, link = await deps["create_text_invoice_booking"](dict(op_row), parsed, location)
        async with pool.acquire() as conn:
            updated = await conn.fetchrow("""
                UPDATE boat_requests
                SET status='assigned', assigned_operator_id=$1, accepted_offer_id=$2,
                    invoice_booking_id=$3, updated_at=NOW()
                WHERE id=$4 AND status NOT IN ('assigned','closed','cancelled')
                RETURNING *
            """, offer["operator_id"], offer_id, booking["id"], req["id"])
            if not updated:
                await reply_message.reply_text("⚠️ Request was already assigned/closed by another action.")
                return True
            await conn.execute("UPDATE boat_request_offers SET status='accepted', accepted_at=NOW() WHERE id=$1", offer_id)
            await conn.execute("UPDATE boat_request_offers SET status='rejected', rejected_at=NOW() WHERE request_id=$1 AND id<>$2 AND status='pending_admin'", req["id"], offer_id)
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_mode TEXT DEFAULT 'operator_direct'")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS customer_price NUMERIC(12,2)")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS operator_cost NUMERIC(12,2)")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS samuga_margin NUMERIC(12,2) DEFAULT 0")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_receiver TEXT DEFAULT 'operator'")
            await conn.execute("ALTER TABLE bookings ADD COLUMN IF NOT EXISTS payment_confirmed_by TEXT")
            await conn.execute("""
                UPDATE bookings
                SET customer_telegram_id=$1, customer_phone=$2,
                    payment_mode=$3, customer_price=$4, operator_cost=$5, samuga_margin=$6,
                    payment_receiver=$7, total_amount=$4
                WHERE id=$8
            """, req["customer_telegram_id"], req.get("customer_contact") or "", payment_mode,
                customer_price_dec, operator_cost, samuga_margin,
                "samuga" if payment_mode == "samuga_managed" else "operator", booking["id"])
            await conn.execute("""
                UPDATE boat_requests
                SET payment_mode=$1, customer_price=$2, operator_cost=$3, samuga_margin=$4
                WHERE id=$5
            """, payment_mode, customer_price_dec, operator_cost, samuga_margin, req["id"])
            await conn.execute("""
                UPDATE boat_request_offers
                SET payment_mode=$1, customer_price=$2, operator_cost=$3, samuga_margin=$4
                WHERE id=$5
            """, payment_mode, customer_price_dec, operator_cost, samuga_margin, offer_id)
            source_note = "manual admin assignment" if offer.get("operator_note") == "Manual admin assignment" else f"offer {offer_id}"
            await _event(conn, req["id"], "admin", admin_user_id, "admin_assigned_operator", f"Assigned {offer['business_name']} {source_note}")
            await _event(conn, req["id"], "system", None, "invoice_created", booking["booking_ref"])
        if payment_mode == "samuga_managed":
            pay_accounts = await _samuga_payment_lines(pool)
            receiver_label = "Samuga Travels"
            review_line = "Your payment will be reviewed by Samuga Travels."
        else:
            pay_accounts = _payment_lines(dict(op_row))
            receiver_label = op_row.get('business_name') or 'Operator'
            review_line = "Your payment will be reviewed by the operator."
        customer_text = (
            f"✅ Boat found!\n\n"
            f"Operator: {op_row.get('business_name')}\n"
            f"Route: {req['route_from']} → {req['route_to']}\n"
            f"Date: {req['travel_date']} @ {req.get('preferred_time') or 'Flexible'}\n"
            f"Passengers: {req.get('passenger_count') or 1}\n"
            f"Trip type: {req.get('trip_type') or 'oneway'}\n"
            f"Price: {offer.get('currency') or 'MVR'} {_money(customer_price_dec)}\n\n"
            f"Transfer to {receiver_label}:\n{pay_accounts}\n\n"
            f"⚠️ Please double-check account number and account name before transfer.\n"
            f"If money is sent to a wrong bank/account, Samuga Travels and the operator cannot refund it. You must contact your bank.\n"
            f"{review_line}\n\n"
            f"Payment link:\n{link}\n\n"
            f"Tap below to continue."
        )
        await ctx.bot.send_message(
            int(req["customer_telegram_id"]),
            customer_text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💳 Pay / Upload Slip", callback_data=f"inv_upload_{booking['id']}")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ])
        )
        try:
            if payment_mode == "samuga_managed":
                op_msg = (
                    f"🚤 Trip assigned by Samuga Travels!\n\n"
                    f"Ref: {req['request_ref']}\n"
                    f"Route: {req['route_from']} → {req['route_to']}\n"
                    f"Date: {req['travel_date']} @ {req.get('preferred_time') or 'Flexible'}\n"
                    f"Passengers: {req.get('passenger_count') or 1}\n\n"
                    f"Customer details will be shared after Samuga confirms payment."
                )
            else:
                op_msg = f"✅ Trip assigned to you!\n\nRef: {req['request_ref']}\nCustomer will receive your payment details and upload slip through Samuga Travels."
            await ctx.bot.send_message(int(op_row["telegram_id"]), op_msg)
        except Exception:
            pass
        await reply_message.reply_text(
            f"✅ Assigned to {op_row.get('business_name')}\n\n"
            f"Invoice/booking created: {booking['booking_ref']}\n"
            f"Payment mode: {'Samuga managed' if payment_mode == 'samuga_managed' else 'Operator direct'}\n"
            f"Customer price: {offer.get('currency') or 'MVR'} {_money(customer_price_dec)}\n"
            f"Operator cost: {offer.get('currency') or 'MVR'} {_money(operator_cost)}\n"
            f"Margin: {offer.get('currency') or 'MVR'} {_money(samuga_margin)}\n\n"
            f"Customer has been sent the payment details and upload button.")
    except Exception as e:
        logger.error(f"Boat request assign/create invoice failed: {e}", exc_info=True)
        await reply_message.reply_text(f"🚨 Assignment failed while creating invoice.\n\nError: {str(e)[:800]}")
    return True

async def handle_boat_request_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict) -> bool:
    if not update.message or not update.message.text:
        return False
    user = update.effective_user
    sd = await deps["get_user_state"](user.id)
    state = sd.get("state")
    temp = sd.get("temp_data", {}) or {}
    text = update.message.text.strip()

    if state == BR_AWAIT_TIME:
        parsed = None
        try:
            parsed = deps.get("parse_time_24hr", lambda x: None)(text)
        except Exception:
            parsed = None
        preferred_time = parsed or text
        await deps["set_user_state"](user.id, BR_AWAIT_CONTACT, {**temp, "preferred_time": preferred_time})
        await update.message.reply_text(
            "✅ Time saved.\n\nSend your contact number for Samuga Travels/operator to reach you.\nExample: 7771234",
            reply_markup=_main_menu_kb())
        return True

    if state == BR_AWAIT_CONTACT:
        await deps["set_user_state"](user.id, BR_AWAIT_PAX, {**temp, "customer_contact": text})
        await update.message.reply_text("👥 How many passengers?\nExample: 6", reply_markup=_main_menu_kb())
        return True

    if state == BR_AWAIT_PAX:
        pax = _parse_int(text)
        if not pax or pax < 1:
            await update.message.reply_text("⚠️ Send a valid passenger count. Example: 6")
            return True
        await deps["set_user_state"](user.id, BR_AWAIT_TRIP_TYPE, {**temp, "passenger_count": pax})
        await update.message.reply_text(
            "What type of trip is this?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➡️ One-way", callback_data="br_trip_oneway")],
                [InlineKeyboardButton("🔁 Return", callback_data="br_trip_return")],
                [InlineKeyboardButton("🛥️ Private hire", callback_data="br_trip_private")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]))
        return True

    if state == BR_AWAIT_NOTES:
        notes = "" if text.lower() in ("skip", "no", "none", "-") else text
        await deps["set_user_state"](user.id, BR_AWAIT_NOTES, {**temp, "notes": notes})
        return await _submit_customer_request(update, ctx, deps)

    if state == ADMIN_AWAIT_BR_MANUAL_PRICE:
        if not deps["is_admin"](user.id, update.message.chat_id):
            await update.message.reply_text("⚠️ Admin only.")
            return True
        price, currency = _parse_price(text)
        request_id = int(temp.get("boat_request_id") or 0)
        operator_id = int(temp.get("operator_id") or 0)
        if not request_id or not operator_id:
            await update.message.reply_text("⚠️ Manual assignment session expired.", reply_markup=_main_menu_kb())
            await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
            return True
        if not price or price <= 0:
            await update.message.reply_text("⚠️ Send the customer price for this trip. Example: 7500 MVR")
            return True
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("SELECT * FROM boat_requests WHERE id=$1", request_id)
            op = await conn.fetchrow("SELECT * FROM operators WHERE id=$1 AND status='approved'", operator_id)
            if not req or req["status"] in ("assigned", "closed", "cancelled"):
                await update.message.reply_text("⚠️ This request is already closed or assigned.", reply_markup=_main_menu_kb())
                await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
                return True
            if not op:
                await update.message.reply_text("⚠️ Operator not found or not approved.", reply_markup=_main_menu_kb())
                await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
                return True
            offer = await conn.fetchrow("""
                INSERT INTO boat_request_offers (request_id, operator_id, price, currency, operator_note, status)
                VALUES ($1,$2,$3,$4,'Manual admin assignment','pending_admin')
                ON CONFLICT (request_id, operator_id)
                DO UPDATE SET price=$3, currency=$4, operator_note='Manual admin assignment', status='pending_admin', created_at=NOW()
                RETURNING *
            """, request_id, operator_id, price, currency)
            await conn.execute("UPDATE boat_requests SET status='offer_received', updated_at=NOW() WHERE id=$1 AND status!='assigned'", request_id)
            await _event(conn, request_id, "admin", user.id, "manual_offer_created", f"Admin selected {op['business_name']} at {currency} {price}")
        await deps["set_user_state"](user.id, ADMIN_AWAIT_BR_MANUAL_PAYMODE, {"offer_id": offer["id"], "customer_price": str(price)})
        await update.message.reply_text(
            "✅ Price saved. Choose payment mode for this request:\n\n"
            "🏦 Operator Direct = customer pays operator.\n"
            "💼 Samuga Managed = customer pays Samuga Travels, then Samuga handles operator.",
            reply_markup=_payment_mode_kb(offer["id"], allow_edit=False)
        )
        return True

    if state == ADMIN_AWAIT_BR_MANAGED_OPERATOR_COST:
        if not deps["is_admin"](user.id, update.message.chat_id):
            await update.message.reply_text("⚠️ Admin only.")
            return True
        cost, currency = _parse_price(text)
        offer_id = int(temp.get("offer_id") or 0)
        customer_price = Decimal(str(temp.get("customer_price") or "0"))
        if not offer_id or not customer_price:
            await update.message.reply_text("⚠️ Assignment session expired.", reply_markup=_main_menu_kb())
            await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
            return True
        if not cost or cost <= 0:
            await update.message.reply_text("⚠️ Send operator cost. Example: 6500 MVR")
            return True
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE boat_request_offers SET price=$1, operator_cost=$1, customer_price=$2 WHERE id=$3", cost, customer_price, offer_id)
        await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
        return await _assign_offer_and_create_invoice(ctx, deps, user.id, offer_id, update.message, payment_mode="samuga_managed", customer_price=customer_price)

    if state == ADMIN_AWAIT_BR_OFFER_CUSTOMER_PRICE:
        if not deps["is_admin"](user.id, update.message.chat_id):
            await update.message.reply_text("⚠️ Admin only.")
            return True
        price, currency = _parse_price(text)
        offer_id = int(temp.get("offer_id") or 0)
        if not offer_id:
            await update.message.reply_text("⚠️ Assignment session expired.", reply_markup=_main_menu_kb())
            await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
            return True
        if not price or price <= 0:
            await update.message.reply_text("⚠️ Send customer price. Example: 7500 MVR")
            return True
        await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
        return await _assign_offer_and_create_invoice(ctx, deps, user.id, offer_id, update.message, payment_mode="samuga_managed", customer_price=price)

    if state == OP_AWAIT_BR_OFFER_PRICE:
        price, currency = _parse_price(text)
        request_id = int(temp.get("boat_request_id") or 0)
        if not request_id:
            await update.message.reply_text("⚠️ Offer session expired.", reply_markup=_main_menu_kb())
            await deps["set_user_state"](user.id, deps.get("OP_IDLE", "op_idle"), {}, role="operator")
            return True
        if not price or price <= 0:
            await update.message.reply_text("⚠️ Send your offer price. Example: 7500 MVR")
            return True
        op = await deps["get_operator"](user.id)
        if not op or op.get("status") != "approved":
            await update.message.reply_text("⚠️ Approved operator account required.")
            return True
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("SELECT * FROM boat_requests WHERE id=$1", request_id)
            if not req or req["status"] in ("assigned", "closed", "cancelled"):
                await update.message.reply_text("⚠️ This request is already closed or assigned.")
                await deps["set_user_state"](user.id, deps.get("OP_IDLE", "op_idle"), {}, role="operator")
                return True
            offer = await conn.fetchrow("""
                INSERT INTO boat_request_offers (request_id, operator_id, price, currency, status)
                VALUES ($1,$2,$3,$4,'pending_admin')
                ON CONFLICT (request_id, operator_id)
                DO UPDATE SET price=$3, currency=$4, status='pending_admin', created_at=NOW()
                RETURNING *
            """, request_id, op["id"], price, currency)
            await conn.execute("UPDATE boat_requests SET status='offer_received', updated_at=NOW() WHERE id=$1 AND status!='assigned'", request_id)
            await _event(conn, request_id, "operator", user.id, "operator_offer_sent", f"{op.get('business_name')} offered {currency} {price}")
        await deps["set_user_state"](user.id, deps.get("OP_IDLE", "op_idle"), {}, role="operator")
        await update.message.reply_text("✅ Offer sent to Samuga Travels. We will notify you if admin assigns the trip to you.", reply_markup=_main_menu_kb())
        try:
            await ctx.bot.send_message(
                deps["ADMIN_GROUP_ID"],
                f"💰 Operator Offer Received\n\n"
                f"Request: {req['request_ref']}\n"
                f"Operator: {op.get('business_name')}\n"
                f"Price: {currency} {_money(price)}\n\n"
                f"Assign this operator?",
                message_thread_id=BOAT_REQUEST_THREAD_ID,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"✅ Assign {op.get('business_name')}", callback_data=f"br_assign_offer_{offer['id']}")],
                    [InlineKeyboardButton("❌ Reject Offer", callback_data=f"br_reject_offer_{offer['id']}")],
                ])
            )
        except Exception as e:
            logger.error(f"Boat request offer admin notify failed: {e}")
        return True

    return False


async def handle_boat_request_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict) -> bool:
    query = update.callback_query
    if not query:
        return False
    user = query.from_user
    data = query.data or ""

    if data == "br_start_request":
        sd = await deps["get_user_state"](user.id)
        t = sd.get("temp_data", {}) or {}
        if not t.get("route_from") or not t.get("route_to") or not t.get("travel_date"):
            await query.message.reply_text("⚠️ Request details expired. Please search your route again.", reply_markup=_main_menu_kb())
            return True
        await deps["set_user_state"](user.id, BR_AWAIT_TIME, t)
        await query.message.reply_text(
            f"🚤 Request a Boat\n\nRoute: {t.get('route_from')} → {t.get('route_to')}\nDate: {t.get('travel_date')}\n\nWhat time do you prefer?\nExample: 16:00 or Evening",
            reply_markup=_main_menu_kb())
        return True

    if data.startswith("br_trip_"):
        sd = await deps["get_user_state"](user.id)
        temp = sd.get("temp_data", {}) or {}
        trip_type = data.replace("br_trip_", "", 1)
        await deps["set_user_state"](user.id, BR_AWAIT_NOTES, {**temp, "trip_type": trip_type})
        await query.message.reply_text(
            "Any notes? Example: pickup Hulhumale, luggage, baby seat, return same day.\n\nType notes or type `skip`.",
            parse_mode="Markdown", reply_markup=_main_menu_kb())
        return True


    if data.startswith("br_manual_assign_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        request_id = int(data.split("_")[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("SELECT * FROM boat_requests WHERE id=$1", request_id)
            if not req or req["status"] in ("assigned", "closed", "cancelled"):
                await query.message.reply_text("⚠️ This request is already closed or assigned.")
                return True
            ops = await conn.fetch("""
                SELECT id, business_name
                FROM operators
                WHERE status='approved'
                  AND telegram_id IS NOT NULL
                  AND COALESCE(subscription_status,'trial') != 'expired'
                ORDER BY is_recommended DESC, business_name ASC
                LIMIT 25
            """)
        if not ops:
            await query.message.reply_text("⚠️ No approved operators found to assign.")
            return True
        buttons = []
        for op in ops:
            name = _clean(op["business_name"] or f"Operator {op['id']}")[:28]
            buttons.append([InlineKeyboardButton(f"✅ {name}", callback_data=f"br_choose_op_{request_id}_{op['id']}")])
        buttons.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
        await query.message.reply_text(
            f"✅ Choose operator to assign\n\nRequest: {req['request_ref']}\nRoute: {req['route_from']} → {req['route_to']}\nDate: {req['travel_date']} @ {req.get('preferred_time') or 'Flexible'}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        return True

    if data.startswith("br_choose_op_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        parts = data.split("_")
        request_id = int(parts[-2])
        operator_id = int(parts[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("SELECT * FROM boat_requests WHERE id=$1", request_id)
            op = await conn.fetchrow("SELECT id, business_name FROM operators WHERE id=$1 AND status='approved'", operator_id)
        if not req or req["status"] in ("assigned", "closed", "cancelled"):
            await query.message.reply_text("⚠️ This request is already closed or assigned.")
            return True
        if not op:
            await query.message.reply_text("⚠️ Operator not found or not approved.")
            return True
        await deps["set_user_state"](user.id, ADMIN_AWAIT_BR_MANUAL_PRICE, {"boat_request_id": request_id, "operator_id": operator_id})
        await query.message.reply_text(
            "💰 Send the customer price for this trip.\n\n"
            "This is the price customer will see on the invoice.\n\n"
            "Example: 7500 MVR",
            reply_markup=_main_menu_kb()
        )
        return True

    if data.startswith("br_ping_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        request_id = int(data.split("_")[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("SELECT * FROM boat_requests WHERE id=$1", request_id)
            if not req:
                await query.message.reply_text("⚠️ Request not found.")
                return True
            ops = await conn.fetch("""
                SELECT id, telegram_id, business_name FROM operators
                WHERE status='approved' AND telegram_id IS NOT NULL
                  AND COALESCE(subscription_status,'trial') != 'expired'
                ORDER BY is_recommended DESC, business_name ASC
            """)
            await conn.execute("UPDATE boat_requests SET status='operators_pinged', updated_at=NOW() WHERE id=$1 AND status NOT IN ('assigned','closed','cancelled')", request_id)
            await _event(conn, request_id, "admin", user.id, "admin_pinged_operators", f"Pinged {len(ops)} operators")
        sent_count = 0
        for op in ops:
            try:
                await ctx.bot.send_message(
                    int(op["telegram_id"]),
                    _operator_ping_text(dict(req)),
                    reply_markup=_operator_ping_kb(request_id)
                )
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed ping operator {op.get('id')}: {e}")
        await query.message.reply_text(f"📣 Ping sent to {sent_count} approved operators.")
        return True

    if data.startswith("br_take_"):
        request_id = int(data.split("_")[-1])
        op = await deps["get_operator"](user.id)
        if not op or op.get("status") != "approved":
            await query.answer("Approved operator account required.", show_alert=True)
            return True
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("SELECT status FROM boat_requests WHERE id=$1", request_id)
        if not req or req["status"] in ("assigned", "closed", "cancelled"):
            await query.message.reply_text("⚠️ This request is already closed or assigned.")
            return True
        await deps["set_user_state"](user.id, OP_AWAIT_BR_OFFER_PRICE, {"boat_request_id": request_id}, role="operator")
        await query.message.reply_text("💰 Send your offer price now.\nExample: 7500 MVR", reply_markup=_main_menu_kb())
        return True

    if data.startswith("br_decline_"):
        request_id = int(data.split("_")[-1])
        try:
            pool = await deps["get_pool"]()
            async with pool.acquire() as conn:
                await _event(conn, request_id, "operator", user.id, "operator_declined", "Operator tapped Not Available")
        except Exception:
            pass
        await query.message.reply_text("No problem bro, marked as not available. 🙏", reply_markup=_main_menu_kb())
        return True

    if data.startswith("br_reject_offer_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        offer_id = int(data.split("_")[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            offer = await conn.fetchrow("SELECT * FROM boat_request_offers WHERE id=$1", offer_id)
            if offer:
                await conn.execute("UPDATE boat_request_offers SET status='rejected', rejected_at=NOW() WHERE id=$1", offer_id)
                await _event(conn, offer["request_id"], "admin", user.id, "offer_rejected", f"Offer {offer_id} rejected")
        await query.message.reply_text("❌ Offer rejected.")
        return True

    if data.startswith("br_close_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        request_id = int(data.split("_")[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            req = await conn.fetchrow("UPDATE boat_requests SET status='closed', closed_at=NOW(), updated_at=NOW() WHERE id=$1 RETURNING *", request_id)
            if req:
                await _event(conn, request_id, "admin", user.id, "request_closed", "Closed by admin")
        await query.message.reply_text("❌ Boat request closed.")
        return True

    if data.startswith("br_assign_offer_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        offer_id = int(data.split("_")[-1])
        await query.message.reply_text(
            "Choose payment mode for this assignment:\n\n"
            "🏦 Operator Direct = customer pays operator.\n"
            "💼 Samuga Managed = customer pays Samuga Travels.\n"
            "Use edit price if you want to add Samuga margin.",
            reply_markup=_payment_mode_kb(offer_id, allow_edit=True)
        )
        return True

    if data.startswith("br_pay_direct_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        offer_id = int(data.split("_")[-1])
        await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
        return await _assign_offer_and_create_invoice(ctx, deps, user.id, offer_id, query.message, payment_mode="operator_direct")

    if data.startswith("br_pay_managed_edit_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        offer_id = int(data.split("_")[-1])
        await deps["set_user_state"](user.id, ADMIN_AWAIT_BR_OFFER_CUSTOMER_PRICE, {"offer_id": offer_id})
        await query.message.reply_text("💰 Send customer price for Samuga Managed payment.\nExample: 7500 MVR", reply_markup=_main_menu_kb())
        return True

    if data.startswith("br_pay_managed_"):
        if not deps["is_admin"](user.id, query.message.chat_id):
            await query.answer("Admin only", show_alert=True)
            return True
        offer_id = int(data.split("_")[-1])
        sd = await deps["get_user_state"](user.id)
        temp2 = sd.get("temp_data", {}) or {}
        if sd.get("state") == ADMIN_AWAIT_BR_MANUAL_PAYMODE and int(temp2.get("offer_id") or 0) == offer_id:
            await deps["set_user_state"](user.id, ADMIN_AWAIT_BR_MANAGED_OPERATOR_COST, temp2)
            await query.message.reply_text(
                "💼 Samuga Managed selected.\n\n"
                "Now send the operator cost / what Samuga will pay operator.\n"
                "Example: 6500 MVR\n\n"
                "Customer will only see the customer price you entered before.",
                reply_markup=_main_menu_kb()
            )
            return True
        await deps["set_user_state"](user.id, deps.get("CX_IDLE", "cx_idle"), {})
        return await _assign_offer_and_create_invoice(ctx, deps, user.id, offer_id, query.message, payment_mode="samuga_managed")


    return False

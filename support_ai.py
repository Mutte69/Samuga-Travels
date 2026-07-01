"""
Samuga Assist — customer/operator support layer for Samuga Travels.
Keeps support logic separate from bot.py.
"""

from __future__ import annotations

from datetime import datetime
import re
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

SUPPORT_AWAIT_ISSUE = "support_await_issue"
SUPPORT_ADMIN_REPLY = "support_admin_reply"


def _ticket_ref(ticket_id: int) -> str:
    return f"SA-{datetime.utcnow().strftime('%y%m%d')}-{ticket_id:04d}"


def _fmt_user(u) -> str:
    name = getattr(u, "full_name", None) or getattr(u, "first_name", None) or "User"
    username = getattr(u, "username", None)
    return f"{name}" + (f" (@{username})" if username else "")


def _support_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Customer Help", callback_data="support_cat_customer"),
         InlineKeyboardButton("🚤 Operator Help", callback_data="support_cat_operator")],
        [InlineKeyboardButton("💳 Payment Issue", callback_data="support_cat_payment"),
         InlineKeyboardButton("🎫 Ticket Issue", callback_data="support_cat_ticket")],
        [InlineKeyboardButton("🚤 Boat Request", callback_data="support_cat_boat_request"),
         InlineKeyboardButton("🧾 Invoice Help", callback_data="support_cat_invoice")],
        [InlineKeyboardButton("🙋 Talk to Human", callback_data="support_human")],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
    ])


def _answer_for(category: str, role: str) -> str:
    answers = {
        "customer": (
            "👤 Customer Help\n\n"
            "To book, search your route, choose date/boat, enter contact and passenger details, then upload your payment slip.\n\n"
            "If no boat is found, tap 🚤 Request a Boat. Samuga Travels will contact operators and notify you when a boat is found."
        ),
        "operator": (
            "🚤 Operator Help\n\n"
            "Operators can add schedules, create invoices, check bookings, confirm payment slips, view passenger lists, and mark passengers boarded.\n\n"
            "If your operator application is pending for more than 24 hours, open Profile in the Mini App and tap 🔔 Ping Samuga Travels."
        ),
        "payment": (
            "💳 Payment Issue\n\n"
            "If your booking is pending, it usually means the payment slip has not been uploaded or has not been confirmed yet.\n\n"
            "Please double-check account number and account name before transfer. If money is sent to a wrong bank/account, Samuga Travels and the operator cannot refund it; you must contact your bank."
        ),
        "ticket": (
            "🎫 Ticket Issue\n\n"
            "Your ticket is sent after the payment slip is checked and confirmed. Normal time is around 5–10 minutes after confirmation.\n\n"
            "If payment was confirmed but no ticket arrived, tap 🙋 Talk to Human and send your booking reference."
        ),
        "boat_request": (
            "🚤 Boat Request Help\n\n"
            "If no scheduled boat is available, submit a boat request with route, date, preferred time, passengers, trip type and contact number.\n\n"
            "Samuga Travels can ping operators or assign an operator manually. When a boat is found, you’ll receive a payment link and status update."
        ),
        "invoice": (
            "🧾 Invoice Help\n\n"
            "Operator invoices create a customer payment link and PDF invoice. The first price entered is the customer price shown on the invoice.\n\n"
            "For Samuga Managed Payment, the customer pays Samuga Travels. For Operator Direct Payment, the customer pays the assigned operator."
        ),
    }
    return "🤖 Samuga Assist\n\n" + answers.get(category, answers["customer"])


async def init_support_db(get_pool):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                ticket_ref TEXT UNIQUE,
                user_telegram_id BIGINT NOT NULL,
                user_name TEXT,
                username TEXT,
                user_type TEXT DEFAULT 'customer',
                booking_ref TEXT,
                category TEXT DEFAULT 'human',
                issue_text TEXT,
                status TEXT DEFAULT 'open',
                assigned_admin_id BIGINT,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                resolved_at TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER REFERENCES support_tickets(id) ON DELETE CASCADE,
                sender_type TEXT,
                sender_telegram_id BIGINT,
                message_text TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)


async def cmd_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict):
    user = update.effective_user
    role = "customer"
    op = await deps["get_operator"](user.id)
    if op and op.get("status") == "approved":
        role = "operator"
    await deps["set_user_state"](user.id, deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)
    await update.effective_message.reply_text(
        "🤖 Samuga Assist\n\n"
        "Hi, I’m Samuga Assist — your Samuga Travels support assistant.\n\n"
        "I can help with bookings, payments, tickets, boat requests, invoices, operator accounts and schedules.\n\n"
        "Choose what you need help with:",
        reply_markup=_support_kb(),
    )


async def _create_support_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict, issue_text: str, category: str = "human"):
    user = update.effective_user
    op = await deps["get_operator"](user.id)
    user_type = "operator" if op and op.get("status") == "approved" else "customer"
    booking_ref = None
    m = re.search(r"\b(ST-\d{6}-\d{3,6}|BR-\d{6}-\d{3,6}|SA-\d{6}-\d{3,6})\b", issue_text, re.I)
    if m:
        booking_ref = m.group(1).upper()

    pool = await deps["get_pool"]()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO support_tickets
            (user_telegram_id, user_name, username, user_type, booking_ref, category, issue_text, status)
            VALUES ($1,$2,$3,$4,$5,$6,$7,'open')
            RETURNING id
        """, user.id, getattr(user, "full_name", None) or getattr(user, "first_name", None), getattr(user, "username", None), user_type, booking_ref, category, issue_text)
        ticket_id = row["id"]
        ref = _ticket_ref(ticket_id)
        await conn.execute("UPDATE support_tickets SET ticket_ref=$1 WHERE id=$2", ref, ticket_id)
        await conn.execute("""
            INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_text)
            VALUES ($1,$2,$3,$4)
        """, ticket_id, user_type, user.id, issue_text)

    text = (
        f"🙋 Human Support Requested\n\n"
        f"Ticket: {ref}\n"
        f"User: {_fmt_user(user)}\n"
        f"Telegram ID: {user.id}\n"
        f"Type: {user_type}\n"
        f"Booking/Request Ref: {booking_ref or 'Not provided'}\n\n"
        f"Issue:\n{issue_text}\n\n"
        f"Status: Open"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply to User", callback_data=f"support_admin_reply_{ticket_id}"),
         InlineKeyboardButton("✅ Mark Resolved", callback_data=f"support_admin_resolve_{ticket_id}")],
    ])
    try:
        await ctx.bot.send_message(
            deps["ADMIN_GROUP_ID"],
            text,
            message_thread_id=deps.get("SUPPORT_THREAD_ID"),
            reply_markup=kb,
        )
    except Exception:
        await ctx.bot.send_message(deps["ADMIN_GROUP_ID"], text, reply_markup=kb)

    await deps["set_user_state"](user.id, deps["OP_IDLE"] if user_type == "operator" else deps["CX_IDLE"], {}, role=user_type)
    await update.effective_message.reply_text(
        f"✅ Support ticket created.\n\n"
        f"Ticket: {ref}\n"
        f"Samuga Travels team has been notified. We’ll reply here in this chat.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]),
    )


async def handle_support_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict) -> bool:
    q = update.callback_query
    if not q:
        return False
    data = q.data or ""
    if not (data.startswith("support_") or data in ("cx_ai_chat", "op_ai_chat", "ai_end_chat")):
        return False

    await q.answer()
    user = q.from_user

    if data in ("support_start", "cx_ai_chat", "op_ai_chat"):
        await cmd_support(update, ctx, deps)
        return True

    if data == "ai_end_chat":
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await deps["set_user_state"](user.id, deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)
        await q.message.reply_text("👋 Samuga Assist closed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
        return True

    if data.startswith("support_cat_"):
        category = data.replace("support_cat_", "", 1)
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await q.message.reply_text(
            _answer_for(category, role),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🙋 Talk to Human", callback_data="support_human")],
                [InlineKeyboardButton("↩️ Support Menu", callback_data="support_start"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]),
        )
        return True

    if data == "support_human":
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await deps["set_user_state"](user.id, SUPPORT_AWAIT_ISSUE, {"support_category": "human"}, role=role)
        await q.message.reply_text(
            "🙋 Talk to Human\n\n"
            "Please describe your issue in one message.\n\n"
            "Add booking/request reference if you have it.\n"
            "Example: ST-260701-5173 payment uploaded but ticket not received.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="main_menu")]]),
        )
        return True

    if data.startswith("support_admin_reply_"):
        if not deps["is_admin"](user.id, update.effective_chat.id if update.effective_chat else 0):
            await q.answer("Admin only", show_alert=True)
            return True
        ticket_id = int(data.rsplit("_", 1)[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            t = await conn.fetchrow("SELECT * FROM support_tickets WHERE id=$1", ticket_id)
        if not t:
            await q.message.reply_text("Ticket not found.")
            return True
        await deps["set_user_state"](user.id, SUPPORT_ADMIN_REPLY, {"ticket_id": ticket_id, "target_user_id": t["user_telegram_id"], "ticket_ref": t["ticket_ref"]}, role="admin")
        await q.message.reply_text(f"💬 Send your reply for ticket {t['ticket_ref']} now.")
        return True

    if data.startswith("support_admin_resolve_"):
        if not deps["is_admin"](user.id, update.effective_chat.id if update.effective_chat else 0):
            await q.answer("Admin only", show_alert=True)
            return True
        ticket_id = int(data.rsplit("_", 1)[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            t = await conn.fetchrow("""
                UPDATE support_tickets SET status='resolved', resolved_at=NOW(), updated_at=NOW(), assigned_admin_id=$2
                WHERE id=$1 RETURNING *
            """, ticket_id, user.id)
        if t:
            await q.message.reply_text(f"✅ Ticket {t['ticket_ref']} marked resolved.")
            try:
                await ctx.bot.send_message(t["user_telegram_id"], f"✅ Your support ticket {t['ticket_ref']} has been marked resolved by Samuga Travels.")
            except Exception:
                pass
        return True

    return False


async def handle_support_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict) -> bool:
    user = update.effective_user
    sd = await deps["get_user_state"](user.id)
    state = sd.get("state")
    text = (update.message.text or "").strip() if update.message else ""

    if state == SUPPORT_AWAIT_ISSUE:
        if not text:
            return True
        if text.lower() in ("cancel", "/cancel", "0"):
            op = await deps["get_operator"](user.id)
            role = "operator" if (op and op.get("status") == "approved") else "customer"
            await deps["set_user_state"](user.id, deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)
            await update.message.reply_text("Cancelled.")
            return True
        await _create_support_ticket(update, ctx, deps, text, "human")
        return True

    if state == SUPPORT_ADMIN_REPLY:
        if not deps["is_admin"](user.id, update.effective_chat.id if update.effective_chat else 0):
            await update.message.reply_text("Admin only.")
            return True
        temp = sd.get("temp_data", {}) or {}
        target = int(temp.get("target_user_id", 0) or 0)
        ticket_id = int(temp.get("ticket_id", 0) or 0)
        ticket_ref = temp.get("ticket_ref", "Support Ticket")
        if not text:
            return True
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE support_tickets SET status='waiting_user', assigned_admin_id=$2, updated_at=NOW() WHERE id=$1
            """, ticket_id, user.id)
            await conn.execute("""
                INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_text)
                VALUES ($1,'admin',$2,$3)
            """, ticket_id, user.id, text)
        await ctx.bot.send_message(
            target,
            f"💬 Samuga Travels Support\n\nTicket: {ticket_ref}\n\n{text}\n\nReply here if you need more help.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🙋 Talk to Human", callback_data="support_human")]]),
        )
        await deps["set_user_state"](user.id, deps["CX_IDLE"], {}, role="admin")
        await update.message.reply_text(f"✅ Reply sent to user for {ticket_ref}.")
        return True

    return False

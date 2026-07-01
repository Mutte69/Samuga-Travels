"""
Samuga Assist — customer/operator support layer for Samuga Travels.
Keeps support logic separate from bot.py.
"""

from __future__ import annotations

from datetime import datetime
import os
import json
import asyncio
import re
import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

SUPPORT_AWAIT_ISSUE = "support_await_issue"
SUPPORT_ADMIN_REPLY = "support_admin_reply"
SUPPORT_AI_CHAT = "support_ai_chat"

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
SUPPORT_AI_ENABLED = os.environ.get("SUPPORT_AI_ENABLED", "true").lower() not in ("0", "false", "no", "off")


def _ticket_ref(ticket_id: int) -> str:
    return f"SA-{datetime.utcnow().strftime('%y%m%d')}-{ticket_id:04d}"


def _fmt_user(u) -> str:
    name = getattr(u, "full_name", None) or getattr(u, "first_name", None) or "User"
    username = getattr(u, "username", None)
    return f"{name}" + (f" (@{username})" if username else "")


def _support_kb() -> InlineKeyboardMarkup:
    # Human handover is still available when the user asks for it or when AI cannot help,
    # but we do not show it as the first option. Samuga Assist should try first.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ask Samuga Assist", callback_data="support_ai_chat")],
        [InlineKeyboardButton("👤 Customer Help", callback_data="support_cat_customer"),
         InlineKeyboardButton("🚤 Operator Help", callback_data="support_cat_operator")],
        [InlineKeyboardButton("💳 Payment Issue", callback_data="support_cat_payment"),
         InlineKeyboardButton("🎫 Ticket Issue", callback_data="support_cat_ticket")],
        [InlineKeyboardButton("🚤 Boat Request", callback_data="support_cat_boat_request"),
         InlineKeyboardButton("🧾 Invoice Help", callback_data="support_cat_invoice")],
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


def _safe_money(v):
    try:
        return float(v) if v is not None else None
    except Exception:
        return None


def _clean_record(d: dict, blocked_keys: set[str]) -> dict:
    out = {}
    for k, v in dict(d or {}).items():
        if k in blocked_keys:
            continue
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif hasattr(v, "isoformat"):
            try:
                out[k] = v.isoformat()
            except Exception:
                out[k] = str(v)
        elif isinstance(v, (int, float, str, bool)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


async def _table_exists(conn, table_name: str) -> bool:
    return bool(await conn.fetchval("SELECT to_regclass($1)", table_name))


async def _support_db_context(deps: dict, telegram_id: int) -> dict:
    """Build a safe, read-only support context for Gemini.
    Never expose Samuga margin/operator cost to customers/operators.
    """
    ctx = {"telegram_id": telegram_id, "operator": None, "recent_bookings": [], "boat_requests": [], "support_tickets": []}
    blocked = {
        "samuga_margin", "operator_cost", "accepted_offer_id", "assigned_admin_id",
        "payment_slip_url", "owner_id_photo_url", "logo_url",
    }
    pool = await deps["get_pool"]()
    async with pool.acquire() as conn:
        try:
            op = await conn.fetchrow("SELECT * FROM operators WHERE telegram_id=$1 ORDER BY id DESC LIMIT 1", telegram_id)
            if op:
                op_d = _clean_record(dict(op), blocked)
                # Bank/account details should not be echoed freely by AI unless payment flow explicitly shows them.
                for bank_key in ("bml_account", "payment_accounts"):
                    if bank_key in op_d:
                        op_d[bank_key] = "saved" if op_d.get(bank_key) else "not saved"
                ctx["operator"] = op_d
        except Exception as e:
            ctx["operator_error"] = str(e)[:160]

        try:
            rows = await conn.fetch("""
                SELECT b.*, o.business_name AS operator_business_name, o.boat_name AS operator_boat_name
                FROM bookings b
                LEFT JOIN operators o ON o.id=b.operator_id
                WHERE b.customer_telegram_id=$1 OR o.telegram_id=$1
                ORDER BY b.created_at DESC
                LIMIT 5
            """, telegram_id)
            for r in rows:
                d = _clean_record(dict(r), blocked)
                ctx["recent_bookings"].append(d)
        except Exception as e:
            ctx["bookings_error"] = str(e)[:160]

        try:
            if await _table_exists(conn, "boat_requests"):
                rows = await conn.fetch("""
                    SELECT br.*, o.business_name AS assigned_operator_name
                    FROM boat_requests br
                    LEFT JOIN operators o ON o.id=br.assigned_operator_id
                    WHERE br.customer_telegram_id=$1
                       OR br.assigned_operator_id IN (SELECT id FROM operators WHERE telegram_id=$1)
                    ORDER BY br.created_at DESC
                    LIMIT 5
                """, telegram_id)
                for r in rows:
                    ctx["boat_requests"].append(_clean_record(dict(r), blocked))
        except Exception as e:
            ctx["boat_requests_error"] = str(e)[:160]

        try:
            if await _table_exists(conn, "support_tickets"):
                rows = await conn.fetch("""
                    SELECT ticket_ref, user_type, booking_ref, category, issue_text, status, created_at, updated_at, resolved_at
                    FROM support_tickets
                    WHERE user_telegram_id=$1
                    ORDER BY created_at DESC
                    LIMIT 3
                """, telegram_id)
                for r in rows:
                    ctx["support_tickets"].append(_clean_record(dict(r), blocked))
        except Exception as e:
            ctx["support_tickets_error"] = str(e)[:160]
    return ctx


def _rulebook() -> str:
    return """
Samuga Travels support rules:
- You are Samuga Assist, the official support assistant for Samuga Travels.
- Help only with Samuga Travels bookings, payments, tickets, invoices, operator accounts, schedules, boat requests, boarding, and app usage.
- Reply in the same language as the user when possible. If the user writes Dhivehi, reply in simple Dhivehi. If mixed Dhivehi/English, reply mixed/simple.
- Use the database context as truth. Do not claim payment is confirmed unless status shows confirmed/paid or the context clearly says so.
- Never reveal Samuga margin, operator cost, internal admin notes, hidden IDs, or private URLs.
- For Samuga Managed Payment, customer pays Samuga Travels and operator gets customer details after admin confirms payment.
- For Operator Direct Payment, customer pays assigned operator and operator confirms the slip.
- If payment is pending: explain slip may be missing or under review.
- If wrong bank/account transfer: Samuga Travels and operator cannot refund; user must contact their bank.
- If no boat is available: tell customer to use Request a Boat and wait for Samuga to contact operators.
- If operator application is pending less than 24 hours: tell them it is under review.
- If operator application is pending more than 24 hours: tell them to open Profile in Mini App and tap “Ping Samuga Travels”.
- Do not show or suggest human support in normal successful answers.
- Only if the user clearly asks for a human/agent/staff/admin OR you cannot answer safely from the database context, end your answer with exactly: HANDOVER_NEEDED
- Keep answers short, warm, professional, and action-focused. Do not invent phone numbers, prices, routes, or policies.
""".strip()


def _gemini_prompt(user_text: str, role: str, db_context: dict) -> str:
    return f"""{_rulebook()}

User role: {role}
Database context JSON:
{json.dumps(db_context, ensure_ascii=False, default=str)[:9000]}

User message:
{user_text}

Write the best support reply now.
If you can answer/help, answer directly and do not mention human support.
If the user clearly asks for a human or you cannot answer safely, end the reply with exactly: HANDOVER_NEEDED"""


def _call_gemini_sync(prompt: str) -> str | None:
    if not (SUPPORT_AI_ENABLED and GEMINI_API_KEY):
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.35, "topP": 0.9, "maxOutputTokens": 360},
    }
    r = requests.post(url, json=payload, timeout=18)
    if r.status_code >= 400:
        return None
    data = r.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None


async def _ask_gemini(user_text: str, role: str, db_context: dict) -> str | None:
    prompt = _gemini_prompt(user_text, role, db_context)
    try:
        return await asyncio.to_thread(_call_gemini_sync, prompt)
    except Exception:
        return None


def _looks_like_human_request(text: str) -> bool:
    t = (text or "").lower()
    keys = ["human", "agent", "staff", "admin", "person", "call me", "talk to", "މީހ", "އެޖެންޓ", "އެހީ"]
    return any(k in t for k in keys)


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
        "Customer Support\n\n"
        "Hi, I’m Samuga Assist — your Samuga Travels support assistant.\n\n"
        "I can help with bookings, payments, tickets, boat requests, invoices, operator accounts and schedules.\n\n"
        "Ask Samuga Assist first. If I can’t help, I’ll connect you to the Samuga Travels team.",
        reply_markup=_support_kb(),
    )


async def cmd_support_ai(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict):
    """Open Samuga Assist directly from the Mini App floating support button."""
    user = update.effective_user
    op = await deps["get_operator"](user.id)
    role = "operator" if (op and op.get("status") == "approved") else "customer"
    await deps["set_user_state"](user.id, SUPPORT_AI_CHAT, {}, role=role)
    status_line = "Smart support is active." if GEMINI_API_KEY else "Smart support key is not set yet, but I can still guide you with support rules."
    await update.effective_message.reply_text(
        "Ask Samuga Assist\n\n"
        f"{status_line}\n\n"
        "Send your question in English, Dhivehi, or mixed Dhivehi-English.\n"
        "Example: I uploaded payment but no ticket yet.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩️ Support Menu", callback_data="support_start"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
        ]),
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
                [InlineKeyboardButton("Ask Samuga Assist", callback_data="support_ai_chat")],
                [InlineKeyboardButton("↩️ Support Menu", callback_data="support_start"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]),
        )
        return True

    if data == "support_ai_chat":
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await deps["set_user_state"](user.id, SUPPORT_AI_CHAT, {}, role=role)
        status_line = "Gemini smart answers are active." if GEMINI_API_KEY else "Smart AI key is not set yet, but I can still guide you with support rules."
        await q.message.reply_text(
            "🤖 Ask Samuga Assist\n\n"
            f"{status_line}\n\n"
            "Send your question in English, Dhivehi, or mixed Dhivehi-English.\n"
            "Example: I uploaded payment but no ticket yet.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("Ask Samuga Assist", callback_data="support_ai_chat")],
                [InlineKeyboardButton("↩️ Support Menu", callback_data="support_start"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]),
        )
        return True

    if data == "support_human":
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await deps["set_user_state"](user.id, SUPPORT_AWAIT_ISSUE, {"support_category": "human"}, role=role)
        await q.message.reply_text(
            "Talk to Human\n\n"
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

    if state == SUPPORT_AI_CHAT:
        if not text:
            return True
        if text.lower() in ("cancel", "/cancel", "0", "menu"):
            op = await deps["get_operator"](user.id)
            role = "operator" if (op and op.get("status") == "approved") else "customer"
            await deps["set_user_state"](user.id, deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)
            await update.message.reply_text("Samuga Assist closed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
            return True
        if _looks_like_human_request(text):
            await _create_support_ticket(update, ctx, deps, text, "human")
            return True
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await update.message.reply_text("Checking your Samuga Travels details...")
        dbctx = await _support_db_context(deps, user.id)
        ans = await _ask_gemini(text, role, dbctx)
        if not ans:
            ans = (
                "Samuga Assist\n\n"
                "I can help with bookings, payments, tickets, boat requests, invoices, and operator accounts.\n\n"
                "Smart support is not active right now. Please choose a support category, or write that you want a human agent."
            )
        needs_human_button = bool(re.search(r"HANDOVER_NEEDED|I\s*(can'?t|cannot)\s+help|not\s+sure|unsure", ans, re.I))
        ans = re.sub(r"\s*HANDOVER_NEEDED\s*", "", ans, flags=re.I).strip()
        rows = []
        if needs_human_button:
            rows.append([InlineKeyboardButton("Contact Samuga Team", callback_data="support_human")])
        rows.append([InlineKeyboardButton("Ask another question", callback_data="support_ai_chat")])
        rows.append([InlineKeyboardButton("↩️ Support Menu", callback_data="support_start"), InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
        await update.message.reply_text(ans[:3900], reply_markup=InlineKeyboardMarkup(rows))
        return True

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
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Contact Samuga Team", callback_data="support_human")]]),
        )
        await deps["set_user_state"](user.id, deps["CX_IDLE"], {}, role="admin")
        await update.message.reply_text(f"✅ Reply sent to user for {ticket_ref}.")
        return True

    return False

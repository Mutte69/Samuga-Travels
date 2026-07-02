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
SUPPORT_HUMAN_CHAT = "support_human_chat"

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
    # Samuga Assist is the main support entry. Keep the customer side clean.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ask Samuga Assist", callback_data="support_ai_chat")],
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
            "Customers should follow the payment details shown on their booking or invoice. Internal payment routing is handled by Samuga Travels."
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
- IMPORTANT LANGUAGE RULE: If the user writes in English, reply ONLY in English. Do not use Romanized Dhivehi words like ge, ah, kurey, kurevey, vaane, mi, thima. If the user writes in Thaana script, reply in simple Thaana Dhivehi. If the user writes Latin-Dhivehi mixed with English, prefer clear English unless the user asks for Dhivehi.
- Use the database context as truth. Do not claim payment is confirmed unless status shows confirmed/paid or the context clearly says so.
- Never reveal Samuga margin, operator cost, internal admin notes, hidden IDs, or private URLs.
- Never reveal internal payment modes, operator cost, Samuga margin, or admin-only payment routing.
- Customers/operators should follow the payment details shown on the booking/invoice and dashboard.
- If payment is pending: explain slip may be missing or under review.
- If wrong bank/account transfer: Samuga Travels and operator cannot refund; user must contact their bank.
- If no boat is available: tell customer to use Request a Boat and wait for Samuga to contact operators.
- For approved operators asking how to add routes/schedules: tell them to open Operator Dashboard or Telegram operator menu and use Add Schedule/Create Schedule/Route tools. If that button is missing, offer human support.
- For operator profile route coverage questions: tell them routes come from schedules and invoices; new invoice/route details may be saved for future search suggestions. If they need admin to enable/edit coverage, offer human support.
- If operator application is pending less than 24 hours: tell them it is under review.
- If operator application is pending more than 24 hours: tell them to open Profile in Mini App and tap “Ping Samuga Travels”.
- Do not show or suggest human support in normal successful answers.
- Only if the user clearly asks for a human/agent/staff/admin OR you cannot answer safely from the database context, end your answer with exactly: HANDOVER_NEEDED
- Keep answers short, warm, professional, and action-focused. Do not invent phone numbers, prices, routes, or policies.
""".strip()


def _gemini_prompt(user_text: str, role: str, db_context: dict) -> str:
    # Build conversation history from rolling window (last 4 turns)
    history = db_context.get("conversation_history") or []
    history_text = ""
    if history:
        turns = []
        for turn in history[-4:]:
            turns.append(f"Customer: {turn.get('user','')}")
            if turn.get('bot'):
                turns.append(f"Samuga Assist: {turn.get('bot','')}")
        history_text = "\n".join(turns)
    else:
        # Fallback to single-turn context for backwards compatibility
        if db_context.get("last_user_message"):
            history_text = (f"Customer: {db_context.get('last_user_message') or ''}\n"
                           f"Samuga Assist: {db_context.get('last_bot_answer') or ''}")

    return f"""{_rulebook()}

User role: {role}
Current support topic: {db_context.get("last_topic") or "none"}

IMPORTANT: Read the full conversation history below before answering.
Never repeat the same answer if the customer is asking a follow-up.
If the customer says "I already did that" or "that didn't work" or refers to a previous message,
acknowledge it and give a DIFFERENT, more specific answer or offer human support.
If you already gave the same answer and the customer is still stuck, always offer: HANDOVER_NEEDED

Conversation so far:
{history_text or "No previous messages."}

Database context JSON:
{json.dumps({k:v for k,v in db_context.items() if k not in ("conversation_history","last_user_message","last_bot_answer")}, ensure_ascii=False, default=str)[:8000]}

Customer's new message:
{user_text}

Write the best support reply now. Be specific and address their exact situation.
If you can answer/help, answer directly and do not mention human support.
If the customer clearly asks for a human, you cannot answer safely, or you have already tried to help with the same issue twice, end the reply with exactly: HANDOVER_NEEDED"""


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



# ─────────────────────────────────────────────────────────────────────────────
# Samuga Assist conversation brain / knowledge base
# ─────────────────────────────────────────────────────────────────────────────
_GREETING_RE = re.compile(r"^(hi|hello|hey|salam|assalaamu alaikum|ހައި|ސަލާމް|އައްސަލާމް)(\s|!|\.|،|$)", re.I)
_SHORT_FOLLOWUP_RE = re.compile(r"^(why|how|where|what|yes|no|ok|okay|ކީއްވެ|ކިހިނެއް|ކޮބައި)$", re.I)


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _reply_language(text: str, previous_language: str | None = None) -> str:
    # Only use Dhivehi when the user actually types Thaana. Mixed Latin/Dhivehi-English stays English.
    if re.search(r"[ހ-޿]", text or ""):
        return "dhivehi"
    return previous_language or "english"


def _detect_support_topic(text: str, last_topic: str | None = None) -> str | None:
    t = _normalize_text(text)
    if not t:
        return None
    if _GREETING_RE.match(t):
        return "greeting"
    if _SHORT_FOLLOWUP_RE.match(t) and last_topic:
        return last_topic
    if any(k in t for k in ["wrong account", "wrong bank", "wrong transfer", "sent money wrong", "wrong number", "ރަނގަޅު ނޫން އެކައުންޓ"]):
        return "wrong_transfer"
    if any(k in t for k in ["payment", "paid", "slip", "transfer", "ޕޭމަންޓ", "ސްލިޕ"]):
        if any(k in t for k in ["pending", "review", "not confirmed", "no ticket", "ޓިކެޓ"]):
            return "payment_pending"
        return "payment_upload"
    if any(k in t for k in ["ticket", "qr", "boarding", "boarded", "ޓިކެޓ"]):
        return "ticket"
    if any(k in t for k in ["operator account", "register as operator", "become operator", "apply as operator", "operator akah", "operator ah", "އޮޕަރޭޓަރ"]):
        if any(k in t for k in ["pending", "review", "approve", "approved", "under review"]):
            return "operator_pending"
        return "operator_register"
    if any(k in t for k in ["add route", "more route", "routes", "add schedule", "schedule", "add trip", "more trips", "add boat", "new route"]):
        return "operator_add_routes"
    if any(k in t for k in ["invoice", "bill", "create invoice", "customer invoice"]):
        return "invoice"
    if any(k in t for k in ["request boat", "no boat", "boat request", "private hire", "find boat"]):
        return "boat_request"
    if any(k in t for k in ["book", "booking", "cheap ticket", "price", "prices", "fare", "search route", "route search"]):
        return "booking_search"
    if any(k in t for k in ["cancel", "refund", "change date", "change booking"]):
        return "cancel_refund"
    if any(k in t for k in ["admin", "human", "agent", "staff", "support team", "person", "talk to"]):
        return "human_support"
    if any(k in t for k in ["samuga managed", "operator direct", "managed payment", "direct payment"]):
        return "payment_upload"
    if any(k in t for k in ["report", "revenue", "monthly", "daily report"]):
        return "reports"
    return None


def _safe_context_flags(db_context: dict) -> dict:
    op = db_context.get("operator") or {}
    bookings = db_context.get("recent_bookings") or []
    boat_requests = db_context.get("boat_requests") or []
    return {
        "is_operator": bool(op),
        "operator_status": str(op.get("status") or "").lower(),
        "operator_name": op.get("business_name") or op.get("owner_name") or "your operator account",
        "has_recent_booking": bool(bookings),
        "latest_booking_status": str((bookings[0] or {}).get("status") or "").lower() if bookings else "",
        "has_boat_request": bool(boat_requests),
    }


def _kb_answer(topic: str | None, text: str, role: str, db_context: dict, last_topic: str | None = None) -> tuple[str | None, str | None, bool]:
    """Return (answer, topic_to_store, needs_human). Uses rules before Gemini so common support is reliable."""
    flags = _safe_context_flags(db_context or {})
    t = _normalize_text(text)
    if topic == "greeting":
        return ("Hi 👋 I’m Samuga Assist. I can help with bookings, payments, tickets, boat requests, invoices, operator accounts, schedules, and app usage. What do you need help with?", last_topic or "general", False)
    if _SHORT_FOLLOWUP_RE.match(t) and not last_topic:
        return ("Sure — what do you need help with? You can ask about a booking, payment, ticket, boat request, invoice, or operator account.", "general", False)
    if topic == "operator_register":
        if flags["is_operator"] and flags["operator_status"] == "approved":
            return (f"You already have an approved operator account for {flags['operator_name']}. To manage it, open Profile → Operator Dashboard. From there you can check bookings, requests, schedules, reports, and payments.", "operator_register", False)
        if flags["is_operator"] and flags["operator_status"] in ("pending", "under_review"):
            return ("Your operator application is under review. If it has been more than 24 hours, open Profile and tap 🔔 Ping Samuga Travels so the team can check it.", "operator_pending", False)
        return ("To register as an operator, open the Samuga Travels Mini App → Profile → Register as Operator / Apply as Operator. Add your business details, boat details, contact number, bank accounts, logo and ID photo if requested. After you submit, Samuga Travels will review and approve the account.", "operator_register", False)
    if topic == "operator_pending":
        return ("Operator applications are reviewed by the Samuga Travels team. If your application is still pending after 24 hours, open Profile in the Mini App and tap 🔔 Ping Samuga Travels. The team will receive an alert to check your application.", "operator_pending", False)
    if topic == "operator_add_routes":
        if flags["is_operator"] and flags["operator_status"] == "approved":
            return ("To add more routes, add schedules/trips for those routes. Open Profile → Operator Dashboard → Schedules/Today → Add Schedule, then enter From, To, date, time, seats, and price. Those schedules help customers find your boat. If you need Samuga Travels to manually edit your route coverage, I can connect you to support.", "operator_add_routes", False)
        return ("Routes are managed through an approved operator account. First register or get your operator account approved. After approval, open Profile → Operator Dashboard and add schedules/trips for the routes you operate.", "operator_add_routes", False)
    if topic == "booking_search":
        return ("To book, search your route in the Mini App or type the route in the Telegram bot, then choose your date, boat, passenger count, and contact details. If no boat is available, tap 🚤 Request a Boat and Samuga Travels will check with operators.", "booking_search", False)
    if topic == "boat_request":
        return ("If no scheduled boat is available, use 🚤 Request a Boat. Add From, To, date, preferred time, passengers, trip type, contact number, pickup/jetty and notes. Samuga Travels will contact operators and notify you when a boat is found.", "boat_request", False)
    if topic == "payment_upload":
        return ("After you transfer payment, open your booking/payment link and tap 💳 Pay / Upload Slip. Upload a clear payment slip. The operator or Samuga Travels team will review it depending on the payment mode.", "payment_upload", False)
    if topic == "payment_pending":
        return ("Your booking stays pending when the payment slip has not been uploaded yet or is still under review. If you already uploaded the slip, please wait for confirmation. If it is urgent or taking too long, I can connect you to Samuga Travels support.", "payment_pending", False)
    if topic == "ticket":
        return ("Your ticket is sent after payment is confirmed. If you uploaded the slip but did not receive a ticket, the payment may still be under review. Check My Trips / My Bookings, or ask support to check your booking reference.", "ticket", False)
    if topic == "wrong_transfer":
        return ("If money was sent to the wrong bank account or wrong account number, Samuga Travels and the operator may not be able to reverse it directly. Please contact your bank immediately with the transaction reference. You can also share the slip with Samuga Travels support so the team can guide you, but refund/reversal must be handled through the bank.", "wrong_transfer", False)
    if topic == "invoice":
        return ("Operators can create an invoice from the Telegram bot by entering customer name, date/time, route, return details if any, and price. The bot creates a PDF invoice and customer payment link. The customer should follow the payment details shown on the invoice/payment screen.", "invoice", False)
    if topic == "payment_modes":
        return ("Please follow the payment details shown on your booking or invoice. Samuga Travels handles the correct payment routing internally, so customers and operators should not rely on hidden payment mode names.", "payment_upload", False)
    if topic == "cancel_refund":
        return ("For cancellation or changes, open your booking and use the available cancel/change option if shown. Refunds depend on booking status, operator policy, payment mode, and whether the trip has already been confirmed. For urgent cancellation, I can connect you to support.", "cancel_refund", False)
    if topic == "reports":
        return ("Operators can view reports in Profile → Operator Dashboard → Report. Reports can include bookings, pending payments, confirmed trips, cancellations, seats sold, and revenue. Admin can see wider reports from the Admin Panel.", "reports", False)
    if topic == "human_support":
        return ("I can connect you to Samuga Travels support. Would you like to talk to a human support agent?", "human_support", True)
    return (None, last_topic, False)

def _ai_answer_is_weak(user_text: str, answer: str) -> bool:
    a = (answer or "").strip()
    u = (user_text or "").strip()
    if not a or len(a) < 35:
        return True
    englishish = bool(re.search(r"[a-zA-Z]", u)) and not re.search(r"[ހ-޿]", u)
    if englishish:
        if re.search(r"[ހ-޿]", a):
            return True
        if re.search(r"\b(ge|ah|eh|koh|kur|kure|kurey|kurevey|vaane|vanee|thima|loabin|mi)\b", a.lower()):
            return True
    weak_phrases = [
        "could you please tell me what you are referring to",
        "please confirm your operator account",
        "please log in to your samuga",
        "i'm not sure", "not fully sure", "cannot answer", "can't answer"
    ]
    return any(p in a.lower() for p in weak_phrases)


async def _send_alert(ctx: ContextTypes.DEFAULT_TYPE, deps: dict, where: str, details: str, user_id: int | None = None):
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        uid = f"\nUser ID: {user_id}" if user_id else ""
        text = f"🚨 Samuga Travels Alert\n\nWhere: {where}{uid}\nTime: {ts}\n\n{str(details)[:1200]}"
        await ctx.bot.send_message(deps["ADMIN_GROUP_ID"], text, message_thread_id=deps.get("ALERT_THREAD_ID"))
    except Exception:
        pass


def _ticket_admin_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Reply to User", callback_data=f"support_admin_reply_{ticket_id}"),
         InlineKeyboardButton("✅ End Session", callback_data=f"support_admin_end_{ticket_id}")],
        [InlineKeyboardButton("✅ Mark Resolved", callback_data=f"support_admin_resolve_{ticket_id}")],
    ])


async def _close_support_session_for_user(deps: dict, user_id: int, user_type: str | None = None):
    role = "operator" if str(user_type or "").lower() == "operator" else "customer"
    await deps["set_user_state"](int(user_id), deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)


async def _get_open_telegram_support_ticket(deps: dict, user_id: int):
    """Return the latest open Telegram support ticket for a user.

    This is a recovery guard. If user_states is accidentally reset while a
    human support ticket is still open, we must still treat incoming private
    messages as support chat and never pass them to route/date booking logic.
    Mini App tickets are excluded because their messages are handled by api.py.
    """
    pool = await deps["get_pool"]()
    async with pool.acquire() as conn:
        return await conn.fetchrow("""
            SELECT * FROM support_tickets
            WHERE user_telegram_id=$1
              AND COALESCE(category,'human') <> 'inapp'
              AND COALESCE(status,'open') NOT IN ('closed','resolved')
            ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
            LIMIT 1
        """, int(user_id))


async def _forward_user_text_to_open_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict, ticket, text: str) -> bool:
    """Forward a Telegram user's message to the active support ticket and return True.
    Handles end/close words too. This function is used by both normal support
    state and recovery guard paths.
    """
    user = update.effective_user
    ticket_id = int(ticket["id"])
    ticket_ref = ticket.get("ticket_ref") or "Support Ticket"
    user_type = str(ticket.get("user_type") or "customer")
    if (text or "").strip().lower() in ("end", "close", "cancel", "/cancel", "0", "menu", "end support", "end chat", "stop support"):
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE support_tickets SET status='closed', resolved_at=NOW(), updated_at=NOW()
                WHERE id=$1 AND user_telegram_id=$2
            """, ticket_id, user.id)
            await conn.execute("""
                INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_text)
                VALUES ($1,'system',$2,$3)
            """, ticket_id, user.id, "Support session ended by user.")
        await _close_support_session_for_user(deps, user.id, user_type)
        await update.message.reply_text("✅ Support chat ended. Returning to main menu.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
        try:
            await ctx.bot.send_message(deps["ADMIN_GROUP_ID"], f"✅ Support session ended by user\n\nTicket: {ticket_ref}", message_thread_id=deps.get("SUPPORT_THREAD_ID"))
        except Exception:
            pass
        return True

    pool = await deps["get_pool"]()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_text)
            VALUES ($1,$2,$3,$4)
        """, ticket_id, user_type, user.id, text)
        await conn.execute("UPDATE support_tickets SET status='waiting_admin', updated_at=NOW() WHERE id=$1", ticket_id)
    # Keep/restore the user's support state so the next message is also captured early.
    await deps["set_user_state"](user.id, SUPPORT_HUMAN_CHAT, {"ticket_id": ticket_id, "ticket_ref": ticket_ref}, role=user_type)
    try:
        await ctx.bot.send_message(
            deps["ADMIN_GROUP_ID"],
            f"💬 New Telegram support message\n\nTicket: {ticket_ref}\nUser: {_fmt_user(user)}\nTelegram ID: {user.id}\n\n{text}\n\nReply from this topic using the button below.",
            message_thread_id=deps.get("SUPPORT_THREAD_ID"),
            reply_markup=_ticket_admin_kb(ticket_id),
        )
    except Exception:
        pass
    # Silent customer-side forwarding: do not add an extra confirmation bubble.
    # The customer already sees their own sent message in Telegram; admin replies
    # will appear normally in this same chat.
    return True


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
        "Ask Samuga Assist about bookings, payments, tickets, boat requests, invoices, operator accounts, schedules, or app usage.\n\n"
        "If I can’t help safely, I’ll offer to connect you to the Samuga Travels team.",
        reply_markup=_support_kb(),
    )


async def cmd_support_ai(update: Update, ctx: ContextTypes.DEFAULT_TYPE, deps: dict):
    """Open Samuga Assist directly from the Mini App floating support button."""
    user = update.effective_user
    op = await deps["get_operator"](user.id)
    role = "operator" if (op and op.get("status") == "approved") else "customer"
    await deps["set_user_state"](user.id, SUPPORT_AI_CHAT, {}, role=role)
    await update.effective_message.reply_text(
        "Ask Samuga Assist\n\n"
        "Tell me what you need help with. I can help with bookings, payments, tickets, boat requests, invoices, operator accounts, schedules, and app usage.\n\n"
        "If I cannot help safely, I’ll offer to connect you to the Samuga Travels team.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ End Support Chat", callback_data="ai_end_chat")],
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
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
    kb = _ticket_admin_kb(ticket_id)
    try:
        await ctx.bot.send_message(
            deps["ADMIN_GROUP_ID"],
            text,
            message_thread_id=deps.get("SUPPORT_THREAD_ID"),
            reply_markup=kb,
        )
    except Exception:
        await ctx.bot.send_message(deps["ADMIN_GROUP_ID"], text, reply_markup=kb)

    await deps["set_user_state"](user.id, SUPPORT_HUMAN_CHAT, {"ticket_id": ticket_id, "ticket_ref": ref}, role=user_type)
    await update.effective_message.reply_text(
        "✅ You’re connected to Samuga Travels support.\n\n"
        "An agent will join this chat as soon as possible. Replies may take around 5–10 minutes. You can keep sending messages here until the session is ended.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ End Support Chat", callback_data=f"support_user_end_{ticket_id}")]]),
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
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]),
        )
        return True

    if data == "support_ai_chat":
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await deps["set_user_state"](user.id, SUPPORT_AI_CHAT, {}, role=role)
        await q.message.reply_text(
            "Ask Samuga Assist\n\n"
            "Tell me what you need help with. I can help with bookings, payments, tickets, boat requests, invoices, operator accounts, schedules, and app usage.\n\n"
            "If I cannot help safely, I’ll offer to connect you to the Samuga Travels team.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ End Support Chat", callback_data="ai_end_chat")],
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")],
            ]),
        )
        return True

    if data.startswith("support_user_end_"):
        ticket_id = int(data.rsplit("_", 1)[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            t = await conn.fetchrow("""
                UPDATE support_tickets SET status='closed', resolved_at=NOW(), updated_at=NOW()
                WHERE id=$1 AND user_telegram_id=$2 RETURNING *
            """, ticket_id, user.id)
        op = await deps["get_operator"](user.id)
        role = "operator" if (op and op.get("status") == "approved") else "customer"
        await deps["set_user_state"](user.id, deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)
        await q.message.reply_text("✅ Support chat ended. Returning to main menu.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
        try:
            if t:
                await ctx.bot.send_message(deps["ADMIN_GROUP_ID"], f"✅ Support session ended by user\n\nTicket: {t['ticket_ref']}", message_thread_id=deps.get("SUPPORT_THREAD_ID"))
        except Exception:
            pass
        return True

    if data.startswith("support_admin_end_"):
        if not deps["is_admin"](user.id, update.effective_chat.id if update.effective_chat else 0):
            await q.answer("Admin only", show_alert=True)
            return True
        ticket_id = int(data.rsplit("_", 1)[-1])
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            t = await conn.fetchrow("""
                UPDATE support_tickets SET status='closed', resolved_at=NOW(), updated_at=NOW(), assigned_admin_id=$2
                WHERE id=$1 RETURNING *
            """, ticket_id, user.id)
            if t:
                await conn.execute("""
                    INSERT INTO support_messages (ticket_id, sender_type, sender_telegram_id, message_text)
                    VALUES ($1,'system',$2,$3)
                """, ticket_id, user.id, "✅ Support chat ended by Samuga Travels team.")
        if not t:
            await q.message.reply_text("Ticket not found.")
            return True
        await _close_support_session_for_user(deps, int(t["user_telegram_id"]), t.get("user_type"))
        if str(t.get("category") or "").lower() != "inapp":
            try:
                await ctx.bot.send_message(
                    t["user_telegram_id"],
                    "✅ Support chat ended by Samuga Travels team.\n\nThank you for contacting Samuga Travels.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]])
                )
            except Exception:
                pass
        await q.message.reply_text(f"✅ Support session ended for {t['ticket_ref']}.")
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
        await deps["set_user_state"](user.id, SUPPORT_ADMIN_REPLY, {"ticket_id": ticket_id, "target_user_id": t["user_telegram_id"], "ticket_ref": t["ticket_ref"], "ticket_category": t.get("category")}, role="admin")
        cat = str(t.get("category") or "").lower()
        if cat == "inapp":
            await q.message.reply_text(
                f"💬 Send your reply for ticket {t['ticket_ref']} now.\n\n"
                "This is a Mini App support ticket, so your reply will appear inside the in-app chat only. It will not DM the user in Telegram."
            )
        else:
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
            await _close_support_session_for_user(deps, int(t["user_telegram_id"]), t.get("user_type"))
            await q.message.reply_text(f"✅ Ticket {t['ticket_ref']} marked resolved and session ended.")
            if str(t.get("category") or "").lower() != "inapp":
                try:
                    await ctx.bot.send_message(t["user_telegram_id"], f"✅ Your support ticket {t['ticket_ref']} has been marked resolved by Samuga Travels.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")]]))
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
        temp = sd.get("temp_data", {}) or {}
        last_topic = temp.get("last_topic")
        last_user_message = temp.get("last_user_message")
        last_bot_answer = temp.get("last_bot_answer")
        dbctx = await _support_db_context(deps, user.id)
        dbctx["last_topic"] = last_topic
        dbctx["last_user_message"] = last_user_message
        dbctx["last_bot_answer"] = last_bot_answer
        topic = _detect_support_topic(text, last_topic)
        kb_ans, kb_topic, kb_human = _kb_answer(topic, text, role, dbctx, last_topic)
        if kb_ans:
            history = temp.get("conversation_history") or []
            history.append({"user": text[:500], "bot": kb_ans[:500]})
            temp.update({"last_topic": kb_topic or topic or last_topic or "general",
                         "last_user_message": text, "last_bot_answer": kb_ans[:900],
                         "conversation_history": history[-6:]})
            await deps["set_user_state"](user.id, SUPPORT_AI_CHAT, temp, role=role)
            rows = []
            if kb_human:
                rows.append([InlineKeyboardButton("Talk to Human", callback_data="support_human")])
            rows.append([InlineKeyboardButton("Ask another question", callback_data="support_ai_chat")])
            rows.append([InlineKeyboardButton("✅ End Support Chat", callback_data="ai_end_chat")])
            rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
            await update.message.reply_text(kb_ans[:3900], reply_markup=InlineKeyboardMarkup(rows))
            return True
        await update.message.reply_text("Checking your Samuga Travels details...")
        try:
            ans = await _ask_gemini(text, role, dbctx)
        except Exception as e:
            await _send_alert(ctx, deps, "Telegram Samuga Assist Gemini", str(e), user.id)
            ans = None
        if not ans:
            await _send_alert(ctx, deps, "Telegram Samuga Assist", "Gemini returned no answer", user.id)
            ans = "I’m having trouble answering this safely. Would you like me to connect you with Samuga Travels support? HANDOVER_NEEDED"
        needs_human_button = bool(re.search(r"HANDOVER_NEEDED|I\s*(can'?t|cannot)\s+help|not\s+sure|unsure", ans, re.I))
        ans = re.sub(r"\s*HANDOVER_NEEDED\s*", "", ans, flags=re.I).strip()
        if _ai_answer_is_weak(text, ans):
            await _send_alert(ctx, deps, "Weak Telegram Samuga Assist answer", f"Question: {text}\n\nAnswer: {ans[:700]}", user.id)
            needs_human_button = True
            ans = "I’m not fully sure about this. Would you like me to connect you with Samuga Travels support?"
        history = temp.get("conversation_history") or []
        history.append({"user": text[:500], "bot": ans[:500]})
        temp.update({"last_topic": topic or last_topic or "general",
                     "last_user_message": text, "last_bot_answer": ans[:900],
                     "conversation_history": history[-6:]})
        await deps["set_user_state"](user.id, SUPPORT_AI_CHAT, temp, role=role)
        rows = []
        if needs_human_button:
            rows.append([InlineKeyboardButton("Talk to Human", callback_data="support_human")])
        rows.append([InlineKeyboardButton("Ask another question", callback_data="support_ai_chat")])
        rows.append([InlineKeyboardButton("✅ End Support Chat", callback_data="ai_end_chat")])
        rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
        await update.message.reply_text(ans[:3900], reply_markup=InlineKeyboardMarkup(rows))
        return True

    if state == SUPPORT_HUMAN_CHAT:
        if not text:
            return True
        temp = sd.get("temp_data", {}) or {}
        ticket_id = int(temp.get("ticket_id", 0) or 0)
        pool = await deps["get_pool"]()
        async with pool.acquire() as conn:
            t = await conn.fetchrow("SELECT * FROM support_tickets WHERE id=$1 AND user_telegram_id=$2", ticket_id, user.id) if ticket_id else None
        if not t or str(t.get("status") or "").lower() in ("closed", "resolved"):
            # Try recovery by looking for another open ticket before releasing the user to normal bot flow.
            t = await _get_open_telegram_support_ticket(deps, user.id)
        if not t:
            op = await deps["get_operator"](user.id)
            role = "operator" if (op and op.get("status") == "approved") else "customer"
            await deps["set_user_state"](user.id, deps["OP_IDLE"] if role == "operator" else deps["CX_IDLE"], {}, role=role)
            await update.message.reply_text("This support session is already closed. You can start a new one with /support.")
            return True
        return await _forward_user_text_to_open_support(update, ctx, deps, t, text)

    # Recovery guard: even if user_states was overwritten/reset while a Telegram
    # support ticket is still open, do NOT let text fall through to booking/search.
    # This prevents messages like "No need to help" becoming route searches.
    if update.effective_chat and getattr(update.effective_chat, "type", "") == "private" and text and not text.startswith("/"):
        t = await _get_open_telegram_support_ticket(deps, user.id)
        if t:
            return await _forward_user_text_to_open_support(update, ctx, deps, t, text)

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
            trow = await conn.fetchrow("SELECT category FROM support_tickets WHERE id=$1", ticket_id)
        ticket_category = str((trow or {}).get("category") or temp.get("ticket_category") or "").lower()
        if ticket_category == "inapp":
            # Mini App tickets stay inside the Mini App chat. Do not send the user a Telegram DM/card for every admin reply.
            await update.message.reply_text(f"✅ Reply saved for {ticket_ref}. User will see it inside the Mini App chat.")
        else:
            # Send only the admin's actual message. The initial support
            # connection message already has an End Support Chat button, and
            # the user can also type "end" / "close" to end the session.
            await ctx.bot.send_message(target, text)
            await update.message.reply_text(f"✅ Reply sent to user for {ticket_ref}.")
        await deps["set_user_state"](user.id, deps["CX_IDLE"], {}, role="admin")
        return True

    return False

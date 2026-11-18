"""Telegram bot: mỗi Forum Topic = một dự án, chat trong topic sẽ chạy Claude Code trong thư mục dự án đó."""

import asyncio
import html
import json
import logging
import os
import re
import secrets
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)
from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BASE_DIR = Path(__file__).resolve().parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env(BASE_DIR / ".env")

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USERS = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x}
PROJECTS_ROOT = Path(os.environ.get("PROJECTS_ROOT", str(Path.home() / "projects"))).expanduser().resolve()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or None  # None = dùng bản claude đi kèm SDK
DEFAULT_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
# Tách "Bash(git:*), Read Bash(npm run test)" thành từng rule, giữ nguyên khoảng trắng trong ngoặc
ALLOWED_TOOLS = re.findall(r"[^\s,()]+(?:\([^)]*\))?", os.environ.get("CLAUDE_ALLOWED_TOOLS", ""))
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT_SECONDS", "900"))
AGY_BIN = os.environ.get("AGY_BIN", "agy")
AGY_SETTINGS = Path(os.environ.get("AGY_SETTINGS",
                                   str(Path.home() / ".gemini/antigravity-cli/settings.json"))).expanduser()
DEFAULT_ENGINE = os.environ.get("DEFAULT_ENGINE", "agy")
ENGINES = {"claude": "Claude Code", "agy": "Antigravity"}
ENGINE_ICONS = {"claude": "📁", "agy": "🪐"}
# Chế độ quyền mặc định khi tạo topic hoặc chuyển engine
ENGINE_DEFAULT_MODE = {
    "claude": DEFAULT_MODE,
    "agy": os.environ.get("AGY_PERMISSION_MODE", "bypassPermissions"),
}
# Trường lưu id phiên của từng engine trong state
SESSION_FIELD = {"claude": "session_id", "agy": "agy_conversation_id"}
STATE_FILE = BASE_DIR / "state.json"

PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypassPermissions")
MODE_LABELS = {
    "default": "🔐 default – hỏi cả khi sửa file",
    "acceptEdits": "✏️ acceptEdits – tự sửa file, hỏi lệnh shell",
    "plan": "📝 plan – chỉ lập kế hoạch",
    "bypassPermissions": "⚠️ bypassPermissions – không hỏi gì",
}
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TG_LIMIT = 3500
PAGE_SIZE = 8
TOPIC_HINT = ("Cần bật Threaded Mode cho bot trong @BotFather (chat riêng), "
              "hoặc Supergroup bật Topics + bot là admin có quyền Manage Topics.")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("claude-telegram")


# ---------- State: chat_id:thread_id -> {project, engine, mode, session_id, agy_conversation_id} ----------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"topics": {}}


STATE = load_state()


def save_state() -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def topic_key(chat_id: int, thread_id: int | None) -> str:
    return f"{chat_id}:{thread_id or 0}"


def thread_of(update: Update) -> int | None:
    msg = update.effective_message
    # Trong General, message_thread_id có thể là id của tin được reply, nên phải kiểm tra is_topic_message
    return msg.message_thread_id if msg and msg.is_topic_message else None


def get_topic(update: Update) -> dict | None:
    return STATE["topics"].get(topic_key(update.effective_chat.id, thread_of(update)))


def engine_of(t: dict) -> str:
    return t.get("engine", "claude")  # topic tạo trước khi có agy là Claude


# ---------- Phiên đang chạy và yêu cầu duyệt quyền đang chờ ----------

class ClaudeRun:
    def __init__(self, client: ClaudeSDKClient):
        self.client = client

    async def stop(self) -> None:
        await self.client.interrupt()


class AgyRun:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc

    async def stop(self) -> None:
        self.proc.terminate()


RUNNING: dict[str, ClaudeRun | AgyRun] = {}
# token -> {"key", "future", "suggestions", "message"}
PENDING: dict[str, dict] = {}


def authorized(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USERS)


# ---------- Markdown -> Telegram HTML ----------

def md_to_html(text: str) -> str:
    out, pos = [], 0
    for m in re.finditer(r"```(\w*)\n?(.*?)```", text, re.S):
        out.append(inline_md(text[pos:m.start()]))
        lang = f' class="language-{m.group(1)}"' if m.group(1) else ""
        out.append(f"<pre><code{lang}>{html.escape(m.group(2).rstrip())}</code></pre>")
        pos = m.end()
    out.append(inline_md(text[pos:]))
    return "".join(out)


def inline_md(text: str) -> str:
    parts = re.split(r"(`[^`\n]+`)", text)
    res = []
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            res.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            s = html.escape(part)
            s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
            s = re.sub(r"^#{1,6} (.+)$", r"<b>\1</b>", s, flags=re.M)
            res.append(s)
    return "".join(res)


def split_markdown(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Chia theo dòng, nếu cắt giữa code block thì đóng/mở lại fence."""
    chunks, cur, fence = [], "", None
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # dòng siêu dài
            line_head, line = line[:limit], line[limit:]
            chunks.append(cur + line_head)
            cur = ""
        if len(cur) + len(line) > limit:
            if fence is not None:
                cur += "```\n"
            chunks.append(cur)
            cur = f"```{fence}\n" if fence is not None else ""
        cur += line
        stripped = line.strip()
        if stripped.startswith("```"):
            fence = None if fence is not None else stripped[3:]
    if cur.strip():
        chunks.append(cur)
    return chunks or ["(trống)"]


async def send_long(context, chat_id: int, thread_id: int | None, text: str) -> None:
    for chunk in split_markdown(text):
        try:
            await context.bot.send_message(chat_id, md_to_html(chunk), message_thread_id=thread_id,
                                           parse_mode=ParseMode.HTML)
        except BadRequest:
            await context.bot.send_message(chat_id, chunk, message_thread_id=thread_id)


# ---------- Danh sách dự án ----------

def list_projects() -> list[Path]:
    if not PROJECTS_ROOT.is_dir():
        return []
    return sorted((p for p in PROJECTS_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")),
                  key=lambda p: p.name.lower())


def projects_keyboard(context: ContextTypes.DEFAULT_TYPE, page: int) -> InlineKeyboardMarkup | None:
    projects = list_projects()
    if not projects:
        return None
    picks = context.bot_data.setdefault("picks", {})
    start = page * PAGE_SIZE
    rows = []
    for p in projects[start:start + PAGE_SIZE]:
        token = secrets.token_hex(4)
        picks[token] = str(p)
        rows.append([InlineKeyboardButton(f"📁 {p.name}", callback_data=f"open:{token}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"page:{page - 1}"))
    if start + PAGE_SIZE < len(projects):
        nav.append(InlineKeyboardButton("▶️", callback_data=f"page:{page + 1}"))
    if nav:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def engine_switch_keyboard(engine: str) -> InlineKeyboardMarkup:
    other = "claude" if engine == "agy" else "agy"
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"🔁 Chuyển sang {ENGINES[other]}", callback_data=f"engine:{other}")]])


def engine_keyboard(current: str | None, action: str = "engine", token: str = "") -> InlineKeyboardMarkup:
    """Nút chọn engine; đánh dấu ✅ engine đang dùng hoặc (mặc định)."""
    rows = []
    for e, name in ENGINES.items():
        mark = " ✅" if e == current else (" (mặc định)" if current is None and e == DEFAULT_ENGINE else "")
        data = f"{action}:{token}:{e}" if token else f"{action}:{e}"
        rows.append([InlineKeyboardButton(f"{ENGINE_ICONS[e]} {name}{mark}", callback_data=data)])
    return InlineKeyboardMarkup(rows)


def mode_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        MODE_LABELS[m] + (" ✅" if m == current else ""), callback_data=f"mode:{m}")] for m in PERMISSION_MODES])


def set_mode(key: str, mode: str) -> str:
    t = STATE["topics"].get(key)
    if not t:
        return "Chưa chọn dự án. Dùng /open."
    t["mode"] = mode
    save_state()
    warn = (f"\n⚠️ {ENGINES[engine_of(t)]} sẽ chạy mọi lệnh không cần hỏi."
            if mode == "bypassPermissions" else "")
    return f"Chế độ quyền: {mode}{warn}"


def welcome_text(t: dict, where: str, chat_mode: bool) -> str:
    engine = engine_of(t)
    other = "claude" if engine == "agy" else "agy"
    resumed = ("\nTiếp tục phiên cũ của dự án này (/reset để bắt đầu mới)."
               if t.get(SESSION_FIELD[engine]) else "")
    return (
        f"✅ {where} dự án <code>{html.escape(t['project'])}</code>\n"
        f"Engine: <b>{ENGINES[engine]}</b> · Chế độ quyền: <code>{t['mode']}</code>{resumed}\n\n"
        f"Cứ nhắn yêu cầu, {ENGINES[engine]} sẽ làm việc trong thư mục này.\n"
        f"🔁 Đổi engine: bấm nút dưới hoặc gõ <code>/engine {other}</code> "
        f"(Claude chạy <code>{ENGINE_DEFAULT_MODE['claude']}</code>, "
        f"Antigravity chạy <code>{ENGINE_DEFAULT_MODE['agy']}</code>)."
        + ("\n📂 Gõ /open để chuyển dự án khác." if chat_mode else "")
    )


async def bind_project(context, chat_id: int, thread_id: int | None, project: Path,
                       engine: str | None = None) -> int | None:
    """Gắn dự án vào topic hiện tại nếu là topic chưa gắn, ngược lại tạo topic mới.
    Chat không hỗ trợ topic thì gắn thẳng vào chat (chuyển dự án). Trả về thread_id."""
    chat_mode = False
    old_here = STATE["topics"].get(topic_key(chat_id, thread_id))
    engine = engine or (engine_of(old_here) if old_here else DEFAULT_ENGINE)
    if thread_id is None or old_here:
        try:
            topic = await context.bot.create_forum_topic(chat_id, f"{ENGINE_ICONS[engine]} {project.name}"[:128])
            thread_id = topic.message_thread_id
        except BadRequest as e:
            if thread_id is not None:
                raise
            log.info("Chat %s không tạo được topic (%s), gắn dự án vào chat", chat_id, e.message)
            chat_mode = True
    key = topic_key(chat_id, thread_id)
    if chat_mode and key in RUNNING:
        await context.bot.send_message(chat_id, "⏳ Đang chạy, đợi xong hoặc /stop rồi mới chuyển dự án.")
        return None
    # Nhớ phiên của từng dự án để chuyển qua lại không mất ngữ cảnh
    sessions = STATE.setdefault("sessions", {})
    old = STATE["topics"].get(key)
    if old:
        sessions[f"{key}|{old['project']}"] = {f: old.get(f) for f in SESSION_FIELD.values()}
    saved = sessions.get(f"{key}|{project}") or {}
    if isinstance(saved, str):  # định dạng cũ: chỉ có session Claude
        saved = {"session_id": saved}
    same_engine = old and engine_of(old) == engine
    STATE["topics"][key] = {
        "project": str(project),
        "engine": engine,
        "mode": old.get("mode", ENGINE_DEFAULT_MODE[engine]) if same_engine else ENGINE_DEFAULT_MODE[engine],
        **{f: saved.get(f) for f in SESSION_FIELD.values()},
    }
    save_state()
    t = STATE["topics"][key]
    where = "Chat này đang làm" if chat_mode else "Topic này gắn với"
    await context.bot.send_message(chat_id, welcome_text(t, where, chat_mode), message_thread_id=thread_id,
                                   parse_mode=ParseMode.HTML, reply_markup=engine_switch_keyboard(engine))
    return thread_id


async def switch_engine(key: str, engine: str) -> str:
    """Đổi engine của topic, chế độ quyền về mặc định của engine mới. Trả về câu thông báo."""
    t = STATE["topics"].get(key)
    if not t:
        return "Chưa chọn dự án. Dùng /open."
    if key in RUNNING:
        return "⏳ Đang chạy, đợi xong hoặc /stop rồi mới đổi engine."
    if engine_of(t) == engine:
        return f"Đang dùng {ENGINES[engine]} rồi."
    t["engine"] = engine
    t["mode"] = ENGINE_DEFAULT_MODE[engine]
    save_state()
    cont = " (tiếp tục phiên cũ)" if t.get(SESSION_FIELD[engine]) else ""
    return f"🔁 Đã chuyển sang {ENGINES[engine]}{cont} · chế độ quyền: {t['mode']}"


# ---------- Commands ----------

HELP = (
    "<b>Claude Code / Antigravity qua Telegram</b>\n\n"
    "<b>Chọn dự án</b> (gõ ở chat chính/General, hoặc trong một topic chưa gắn dự án).\n"
    "Chat có topic: mỗi dự án một topic. Chat không có topic: /open để chuyển dự án.\n"
    "/new &lt;tên&gt; [claude|agy] – tạo dự án mới (thư mục mới)\n"
    "/open – chọn dự án có sẵn\n\n"
    "<b>Khi đã chọn dự án:</b>\n"
    "nhắn bất kỳ – gửi yêu cầu cho engine đang chọn\n"
    "/engine [claude|agy] – đổi engine (Claude Code / Antigravity)\n"
    "khi Claude xin quyền: bấm nút, hoặc nhắn lời góp ý để từ chối kèm hướng dẫn\n"
    "/stop – dừng tác vụ đang chạy\n"
    "/reset – bắt đầu phiên mới (quên ngữ cảnh)\n"
    "/mode [default|acceptEdits|plan|bypassPermissions] – chế độ quyền\n"
    "/info – thông tin topic\n"
)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    msg = update.effective_message
    key = topic_key(update.effective_chat.id, thread_of(update))
    if not context.args:
        # Hỏi tên: tin nhắn kế tiếp của user này trong topic này là tên dự án
        context.bot_data.setdefault("await_new", {})[key] = update.effective_user.id
        await msg.reply_text("📝 Nhập tên dự án mới (chữ, số, . _ -):",
                             reply_markup=ForceReply(selective=True, input_field_placeholder="ten-du-an"))
        return
    context.bot_data.get("await_new", {}).pop(key, None)
    engine = context.args[1].lower() if len(context.args) > 1 else None
    if engine and engine not in ENGINES:
        await msg.reply_text("Engine chỉ có: claude hoặc agy.")
        return
    await new_project_step(context, msg, update.effective_chat.id, thread_of(update), context.args[0], engine)


async def new_project_step(context, msg, chat_id: int, thread_id: int | None, name: str,
                           engine: str | None) -> None:
    """Kiểm tra tên; chưa có engine thì hiện nút chọn engine, có rồi thì tạo dự án."""
    if not PROJECT_NAME_RE.match(name):
        await msg.reply_text("Tên chỉ gồm chữ, số, . _ - (tối đa 64 ký tự). Gõ /new để thử lại.")
        return
    if (PROJECTS_ROOT / name).exists():
        await msg.reply_text(f"Dự án '{name}' đã tồn tại, dùng /open để chọn.")
        return
    if engine is None:
        token = secrets.token_hex(4)
        context.bot_data.setdefault("new_names", {})[token] = name
        await msg.reply_text(f"Chọn engine cho dự án <code>{html.escape(name)}</code>:",
                             parse_mode=ParseMode.HTML, reply_markup=engine_keyboard(None, "newengine", token))
        return
    await create_project(context, msg, chat_id, thread_id, name, engine)


async def create_project(context, msg, chat_id: int, thread_id: int | None, name: str, engine: str) -> None:
    project = PROJECTS_ROOT / name
    if project.exists():
        await msg.reply_text(f"Dự án '{name}' đã tồn tại, dùng /open để chọn.")
        return
    project.mkdir(parents=True)
    try:
        await bind_project(context, chat_id, thread_id, project, engine)
    except BadRequest as e:
        await msg.reply_text(f"Không tạo được topic: {e.message}\n" + TOPIC_HINT)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    kb = projects_keyboard(context, 0)
    if kb is None:
        await update.effective_message.reply_text(f"Chưa có dự án nào trong {PROJECTS_ROOT}. Dùng /new <tên>.")
        return
    await update.effective_message.reply_text(f"Chọn dự án trong <code>{html.escape(str(PROJECTS_ROOT))}</code>:",
                                              reply_markup=kb, parse_mode=ParseMode.HTML)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not authorized(update):
        await query.answer("Không có quyền.", show_alert=True)
        return
    action, _, arg = query.data.partition(":")
    if action == "perm":
        await on_permission_answer(query, arg)
        return
    if action == "agyperm":
        await on_agy_permission(query, context, arg)
        return
    if action == "engine":
        if arg not in ENGINES:
            await query.answer()
            return
        thread_id = query.message.message_thread_id if query.message.is_topic_message else None
        text = await switch_engine(topic_key(query.message.chat.id, thread_id), arg)
        await query.answer(text[:190])
        if not text.startswith("🔁"):
            return
        try:  # cập nhật dấu ✅ trên bảng chọn
            await query.edit_message_reply_markup(engine_keyboard(arg))
        except BadRequest:
            pass
        await context.bot.send_message(query.message.chat.id, text, message_thread_id=thread_id)
        return
    if action == "mode":
        thread_id = query.message.message_thread_id if query.message.is_topic_message else None
        key = topic_key(query.message.chat.id, thread_id)
        if arg not in PERMISSION_MODES or key not in STATE["topics"]:
            await query.answer("Chưa chọn dự án.", show_alert=True)
            return
        text = set_mode(key, arg)
        await query.answer(text.split("\n")[0])
        await query.edit_message_text(text, reply_markup=mode_keyboard(arg))
        return
    if action == "newengine":
        token, _, engine = arg.partition(":")
        name = context.bot_data.get("new_names", {}).pop(token, None)
        if not name or engine not in ENGINES:
            await query.answer("Lựa chọn đã cũ, gõ /new lại.", show_alert=True)
            return
        await query.answer()
        await query.edit_message_text(f"Tạo dự án {name} với {ENGINES[engine]}...")
        thread_id = query.message.message_thread_id if query.message.is_topic_message else None
        await create_project(context, query.message, query.message.chat.id, thread_id, name, engine)
        return
    if action == "page":
        await query.answer()
        await query.edit_message_reply_markup(projects_keyboard(context, int(arg)))
        return
    if action == "open":
        path = context.bot_data.get("picks", {}).get(arg)
        if not path or not Path(path).is_dir():
            await query.answer("Danh sách đã cũ, gõ /open lại.", show_alert=True)
            return
        await query.answer()
        try:
            new_thread = await bind_project(context, query.message.chat.id,
                                            query.message.message_thread_id if query.message.is_topic_message else None,
                                            Path(path))
            await query.edit_message_text(f"Đã mở {Path(path).name}" +
                                          ("" if new_thread is None else " → xem topic mới."))
        except BadRequest as e:
            await query.edit_message_text(f"Không tạo được topic: {e.message}\n" +
                                          TOPIC_HINT)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    t = get_topic(update)
    if not t:
        await update.effective_message.reply_text("Chưa chọn dự án. Dùng /open hoặc /new <tên>.")
        return
    key = topic_key(update.effective_chat.id, thread_of(update))
    engine = engine_of(t)
    await update.effective_message.reply_text(
        f"{ENGINE_ICONS[engine]} <code>{html.escape(t['project'])}</code>\n"
        f"Engine: <b>{ENGINES[engine]}</b>\n"
        f"Phiên: <code>{t.get(SESSION_FIELD[engine]) or '(mới)'}</code>\n"
        f"Chế độ quyền: <code>{t.get('mode', DEFAULT_MODE)}</code>\n"
        f"Đang chạy: {'có' if key in RUNNING else 'không'}",
        parse_mode=ParseMode.HTML)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    t = get_topic(update)
    if not t:
        return
    t[SESSION_FIELD[engine_of(t)]] = None
    save_state()
    await update.effective_message.reply_text(f"🔄 Đã bắt đầu phiên {ENGINES[engine_of(t)]} mới.")


async def cmd_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    t = get_topic(update)
    if not t:
        await update.effective_message.reply_text("Chưa chọn dự án. Dùng /open.")
        return
    engine = context.args[0].lower() if context.args else ""
    if engine not in ENGINES:
        await update.effective_message.reply_text(
            f"Đang dùng: {ENGINES[engine_of(t)]}. Chọn engine:",
            reply_markup=engine_keyboard(engine_of(t)))
        return
    text = await switch_engine(topic_key(update.effective_chat.id, thread_of(update)), engine)
    await update.effective_message.reply_text(text)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    t = get_topic(update)
    if not t:
        await update.effective_message.reply_text("Chưa chọn dự án. Dùng /open.")
        return
    if not context.args or context.args[0] not in PERMISSION_MODES:
        current = t.get("mode", DEFAULT_MODE)
        await update.effective_message.reply_text(
            f"Chế độ quyền hiện tại: {current}. Chọn:", reply_markup=mode_keyboard(current))
        return
    key = topic_key(update.effective_chat.id, thread_of(update))
    await update.effective_message.reply_text(set_mode(key, context.args[0]))


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    key = topic_key(update.effective_chat.id, thread_of(update))
    run = RUNNING.get(key)
    if not run:
        await update.effective_message.reply_text("Không có tác vụ nào đang chạy.")
        return
    context.bot_data.setdefault("stopped", set()).add(key)
    for p in [p for p in PENDING.values() if p["key"] == key]:
        if not p["future"].done():
            p["future"].set_result(PermissionResultDeny(message="Người dùng đã dừng tác vụ.", interrupt=True))
    await run.stop()
    await update.effective_message.reply_text("⏹ Đã gửi lệnh dừng.")


# ---------- Duyệt quyền ----------

def describe_tool(name: str, inp: dict) -> str:
    for field in ("command", "CommandLine", "file_path", "TargetFile", "AbsolutePath", "pattern", "Query",
                  "url", "Url", "description", "prompt"):
        if field in inp:
            value = str(inp[field]).replace("\n", " ")
            return f"🔧 {name}: {value[:120]}"
    return f"🔧 {name}"


def permission_details(name: str, inp: dict) -> str:
    """Nội dung (HTML) cho tin nhắn xin quyền, hiển thị phần quan trọng nhất của từng tool."""
    def pre(text: str, limit: int = 1500) -> str:
        text = str(text)
        if len(text) > limit:
            text = text[:limit] + "\n…"
        return f"<pre>{html.escape(text)}</pre>"

    if name == "Bash":
        desc = f"<i>{html.escape(inp['description'])}</i>\n" if inp.get("description") else ""
        return desc + pre(inp.get("command", ""))
    if name == "Edit":
        return (f"<code>{html.escape(inp.get('file_path', ''))}</code>\n"
                f"➖{pre(inp.get('old_string', ''), 700)}➕{pre(inp.get('new_string', ''), 700)}")
    if name == "Write":
        return f"<code>{html.escape(inp.get('file_path', ''))}</code>\n{pre(inp.get('content', ''), 1200)}"
    if name == "ExitPlanMode" and inp.get("plan"):
        return "📋 <b>Kế hoạch:</b>\n" + md_to_html(inp["plan"][:2500])
    return pre(json.dumps(inp, ensure_ascii=False, indent=2))


def make_permission_handler(context, chat_id: int, thread_id: int | None, key: str, on_wait):
    async def can_use_tool(name: str, inp: dict, ctx: ToolPermissionContext):
        if name == "AskUserQuestion":
            return PermissionResultDeny(
                message="Người dùng đang chat qua Telegram, không hiển thị được hộp chọn. "
                        "Hãy hỏi bằng văn bản trong câu trả lời và dừng lại chờ họ trả lời.")
        token = secrets.token_hex(6)
        buttons = [InlineKeyboardButton("✅ Cho phép", callback_data=f"perm:{token}:allow")]
        if ctx.suggestions:
            buttons.append(InlineKeyboardButton("♾ Luôn cho phép", callback_data=f"perm:{token}:always"))
        buttons.append(InlineKeyboardButton("❌ Từ chối", callback_data=f"perm:{token}:deny"))
        text = f"🔐 <b>Claude xin quyền: {html.escape(name)}</b>\n{permission_details(name, inp)}"
        if ctx.blocked_path:
            text += f"\n📂 Ngoài thư mục dự án: <code>{html.escape(ctx.blocked_path)}</code>"
        try:
            message = await context.bot.send_message(chat_id, text[:4000], message_thread_id=thread_id,
                                                     parse_mode=ParseMode.HTML,
                                                     reply_markup=InlineKeyboardMarkup([buttons]))
        except BadRequest:
            message = await context.bot.send_message(chat_id, f"🔐 Claude xin quyền: {name}\n{inp}"[:4000],
                                                     message_thread_id=thread_id,
                                                     reply_markup=InlineKeyboardMarkup([buttons]))
        future = asyncio.get_running_loop().create_future()
        PENDING[token] = {"key": key, "future": future, "suggestions": ctx.suggestions, "message": message}
        await on_wait(f"🔐 Chờ duyệt: {name}")
        try:
            return await asyncio.wait_for(future, APPROVAL_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                await message.edit_text(f"⌛ Hết thời gian chờ duyệt {name}, đã từ chối.")
            except BadRequest:
                pass
            return PermissionResultDeny(message="Người dùng không phản hồi yêu cầu cấp quyền.")
        finally:
            PENDING.pop(token, None)

    return can_use_tool


async def on_permission_answer(query, arg: str) -> None:
    token, _, choice = arg.partition(":")
    pending = PENDING.get(token)
    if not pending or pending["future"].done():
        await query.answer("Yêu cầu này đã hết hạn.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    if choice == "allow":
        result, label = PermissionResultAllow(), "✅ Đã cho phép"
    elif choice == "always":
        # Lưu rule vào .claude/settings.local.json của dự án để các lần sau không hỏi lại
        for s in pending["suggestions"]:
            if s.type == "addRules":
                s.destination = "localSettings"
        result = PermissionResultAllow(updated_permissions=pending["suggestions"])
        label = "♾ Đã cho phép luôn (lưu trong dự án)"
    else:
        result, label = PermissionResultDeny(message="Người dùng từ chối."), "❌ Đã từ chối"
    pending["future"].set_result(result)
    await query.answer(label)
    original = query.message.text_html or ""
    try:
        await query.edit_message_text(f"{original}\n\n<b>{label}</b>"[:4096], parse_mode=ParseMode.HTML)
    except BadRequest:
        await query.edit_message_reply_markup(None)


async def deny_with_feedback(key: str, text: str) -> bool:
    """Nhắn chữ khi đang có yêu cầu chờ duyệt = từ chối kèm hướng dẫn cho Claude."""
    for pending in PENDING.values():
        if pending["key"] == key and not pending["future"].done():
            pending["future"].set_result(PermissionResultDeny(message=f"Người dùng từ chối và nhắn: {text}"))
            try:
                await pending["message"].edit_reply_markup(None)
            except BadRequest:
                pass
            return True
    return False


# ---------- Chạy engine ----------

class Progress:
    """Tin nhắn trạng thái được sửa liên tục trong lúc engine làm việc."""

    def __init__(self, status):
        self.status = status
        self.steps: list[str] = []
        self.last_edit = 0.0
        self.started = time.monotonic()

    @property
    def elapsed(self) -> int:
        return int(time.monotonic() - self.started)

    async def add(self, line: str, force: bool = False) -> None:
        self.steps.append(line)
        await self.refresh(force)

    async def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self.last_edit < 2.5:
            return
        self.last_edit = time.monotonic()
        body = "\n".join(self.steps[-12:]) or "..."
        try:
            await self.status.edit_text(f"⏳ Đang làm ({self.elapsed}s)\n{body}"[:TG_LIMIT])
        except BadRequest:
            pass

    async def finish(self, text: str) -> None:
        try:
            await self.status.edit_text(text)
        except BadRequest:
            pass


def was_stopped(context, key: str) -> bool:
    stopped = context.bot_data.get("stopped", set())
    result = key in stopped
    stopped.discard(key)
    return result


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        return
    msg = update.effective_message
    chat_id, thread_id = update.effective_chat.id, thread_of(update)
    key = topic_key(chat_id, thread_id)
    awaiting = context.bot_data.get("await_new", {})
    if awaiting.get(key) == update.effective_user.id and key not in RUNNING:
        del awaiting[key]
        await new_project_step(context, msg, chat_id, thread_id, msg.text.strip(), None)
        return
    t = STATE["topics"].get(key)
    if not t:
        if update.effective_chat.type == "private" or thread_id is not None:
            await msg.reply_text("Chưa chọn dự án. Dùng /open để chọn hoặc /new <tên> để tạo mới.")
        return
    if key in RUNNING:
        if await deny_with_feedback(key, msg.text):
            await msg.reply_text("❌ Đã từ chối và chuyển lời nhắn cho Claude.")
        else:
            await msg.reply_text(f"⏳ {ENGINES[engine_of(t)]} đang làm việc. Đợi xong hoặc /stop.")
        return
    if engine_of(t) == "agy":
        await run_agy(context, chat_id, thread_id, key, t, msg.text)
    else:
        await run_claude(context, chat_id, thread_id, key, t, msg.text)


async def run_claude(context, chat_id: int, thread_id: int | None, key: str, t: dict, prompt: str) -> None:
    status = await context.bot.send_message(chat_id, "⏳ Claude đang xử lý...", message_thread_id=thread_id)
    progress = Progress(status)
    stderr_lines: list[str] = []

    async def on_wait(line: str) -> None:
        await progress.add(line, force=True)

    options = ClaudeAgentOptions(
        cwd=t["project"],
        permission_mode=t.get("mode", DEFAULT_MODE),
        resume=t.get("session_id"),
        allowed_tools=ALLOWED_TOOLS,
        can_use_tool=make_permission_handler(context, chat_id, thread_id, key, on_wait),
        cli_path=CLAUDE_BIN,
        stderr=lambda line: stderr_lines.append(line),
    )
    client = ClaudeSDKClient(options)
    RUNNING[key] = ClaudeRun(client)
    result: ResultMessage | None = None
    error: Exception | None = None
    try:
        await client.connect()
        await client.query(prompt)
        async for m in client.receive_response():
            if isinstance(m, SystemMessage) and m.subtype == "init" and m.data.get("session_id"):
                t["session_id"] = m.data["session_id"]
                save_state()
            elif isinstance(m, AssistantMessage):
                for block in m.content:
                    if isinstance(block, ToolUseBlock):
                        progress.steps.append(describe_tool(block.name, block.input))
                    elif isinstance(block, TextBlock) and block.text.strip():
                        progress.steps.append(f"💬 {block.text.strip().splitlines()[0][:120]}")
                await progress.refresh()
            elif isinstance(m, ResultMessage):
                result = m
    except Exception as e:  # lỗi kết nối/tiến trình CLI
        log.exception("Claude run failed")
        error = e
    finally:
        RUNNING.pop(key, None)
        for p in [p for p in PENDING.values() if p["key"] == key]:
            if not p["future"].done():
                p["future"].cancel()
        try:
            await client.disconnect()
        except Exception:
            pass

    stopped = was_stopped(context, key)
    ok = result is not None and not result.is_error and not stopped
    await progress.finish(f"✅ Xong sau {progress.elapsed}s, {len(progress.steps)} bước" if ok
                          else f"{'⏹' if stopped else '❌'} Kết thúc sau {progress.elapsed}s")

    if stopped:
        await context.bot.send_message(chat_id, "⏹ Đã dừng.", message_thread_id=thread_id)
        return
    if result is None:
        log_tail = "".join(stderr_lines)[-2500:] or "(không có log)"
        await send_long(context, chat_id, thread_id, f"❌ Lỗi khi chạy Claude: {error}\n```\n{log_tail}\n```")
        return

    if result.session_id:
        t["session_id"] = result.session_id
        save_state()

    text = result.result or "(Claude không trả lời văn bản)"
    if result.is_error:
        text = f"❌ Lỗi: {text}\n\nNếu phiên bị hỏng, thử /reset."
    footer = []
    if result.permission_denials:
        footer.append(f"⚠️ {len(result.permission_denials)} thao tác bị từ chối quyền.")
    if result.total_cost_usd is not None:
        footer.append(f"💲 {result.total_cost_usd:.4f} USD · {result.num_turns} lượt")
    if footer:
        text += "\n\n" + "\n".join(footer)
    await send_long(context, chat_id, thread_id, text)


# ---------- Antigravity (agy) ----------

def agy_mode_flags(mode: str, skip: bool) -> list[str]:
    if skip or mode == "bypassPermissions":
        return ["--dangerously-skip-permissions"]
    if mode == "acceptEdits":
        return ["--mode", "accept-edits"]
    if mode == "plan":
        return ["--mode", "plan"]
    return []


async def run_agy(context, chat_id: int, thread_id: int | None, key: str, t: dict, prompt: str,
                  skip_permissions: bool = False) -> None:
    cmd = [AGY_BIN, "-p", prompt, "--output-format", "stream-json"]
    if t.get("agy_conversation_id"):
        cmd += ["--conversation", t["agy_conversation_id"]]
    cmd += agy_mode_flags(t.get("mode", ENGINE_DEFAULT_MODE["agy"]), skip_permissions)

    status = await context.bot.send_message(chat_id, "⏳ Antigravity đang xử lý...", message_thread_id=thread_id)
    progress = Progress(status)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=t["project"], stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
    except OSError as e:
        await progress.finish(f"❌ Không chạy được {AGY_BIN}: {e}")
        return
    RUNNING[key] = AgyRun(proc)
    stderr_task = asyncio.create_task(proc.stderr.read())  # đọc song song để stderr đầy không làm treo
    texts: dict[int, str] = {}
    seen_tools: set[int] = set()
    denied: list[dict] = []  # {"kind": "command"|"other", "target": ...}
    result: dict | None = None
    try:
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = ev.get("event")
            if kind == "init" and ev.get("conversation_id"):
                t["agy_conversation_id"] = ev["conversation_id"]
                save_state()
            elif kind == "step_update":
                su = ev.get("step_update") or {}
                idx, state, stype = su.get("step_index", 0), su.get("state"), su.get("step_type")
                if stype == "tool":
                    info = su.get("tool_info") or {}
                    name = su.get("tool_name") or info.get("name", "?")
                    params = info.get("parameters") or {}
                    if idx not in seen_tools:
                        seen_tools.add(idx)
                        await progress.add(describe_tool(name, params))
                    if state == "ERROR":
                        err = (info.get("error") or {}).get("message", "")
                        if "permission" in err and "denied" in err:
                            if params.get("CommandLine"):
                                denied.append({"kind": "command", "target": params["CommandLine"]})
                            else:
                                denied.append({"kind": "other", "target": f"{name} {json.dumps(params, ensure_ascii=False)}"})
                            await progress.add(f"⛔ Bị chặn: {denied[-1]['target'][:120]}", force=True)
                        else:
                            await progress.add(f"⚠️ {name} lỗi: {(err.splitlines() or [''])[0][:100]}")
                elif stype == "agent_response" and su.get("text_delta"):
                    texts[idx] = texts.get(idx, "") + su["text_delta"]
                    if state == "DONE" and texts[idx].strip():
                        await progress.add(f"💬 {texts[idx].strip().splitlines()[0][:120]}")
            elif kind == "result":
                result = ev.get("result") or {}
        await proc.wait()
    finally:
        RUNNING.pop(key, None)
    stderr = (await stderr_task).decode(errors="replace")

    stopped = was_stopped(context, key)
    ok = result is not None and result.get("status") == "SUCCESS" and not stopped
    await progress.finish(f"✅ Xong sau {progress.elapsed}s, {len(progress.steps)} bước" if ok
                          else f"{'⏹' if stopped else '❌'} Kết thúc sau {progress.elapsed}s")
    if stopped:
        await context.bot.send_message(chat_id, "⏹ Đã dừng.", message_thread_id=thread_id)
        return
    if result is None:
        await send_long(context, chat_id, thread_id,
                        f"❌ Antigravity thoát với mã {proc.returncode}\n```\n{stderr[-2500:] or '(không có log)'}\n```")
        return

    if result.get("conversation_id"):
        t["agy_conversation_id"] = result["conversation_id"]
        save_state()
    text = result.get("response") or (texts[max(texts)] if texts else "") or "(Antigravity không trả lời văn bản)"
    if result.get("status") != "SUCCESS":
        text = f"❌ {result.get('status')}: {text}\n\nNếu phiên bị hỏng, thử /reset."
    usage = result.get("usage") or {}
    text += f"\n\n🔢 {usage.get('total_tokens', '?')} tokens · {result.get('num_turns', '?')} lượt"
    await send_long(context, chat_id, thread_id, text)

    if not denied:
        denied = [{"kind": "other", "target": a.get("display_name") or a.get("action", "?")}
                  for a in result.get("denied_actions") or []]
    if denied:
        await ask_agy_permission(context, chat_id, thread_id, key, denied)


async def ask_agy_permission(context, chat_id: int, thread_id: int | None, key: str, denied: list[dict]) -> None:
    token = secrets.token_hex(6)
    context.bot_data.setdefault("agy_denied", {})[token] = {
        "key": key, "chat_id": chat_id, "thread_id": thread_id, "denied": denied}
    buttons = [InlineKeyboardButton("✅ Cho phép & chạy tiếp", callback_data=f"agyperm:{token}:allow")]
    if any(d["kind"] == "command" for d in denied):
        buttons.append(InlineKeyboardButton("♾ Luôn cho phép", callback_data=f"agyperm:{token}:always"))
    buttons.append(InlineKeyboardButton("❌ Bỏ qua", callback_data=f"agyperm:{token}:skip"))
    listing = "\n".join(f"• {d['target'][:300]}" for d in denied[:10])
    text = (f"🔐 <b>Antigravity bị chặn {len(denied)} thao tác:</b>\n<pre>{html.escape(listing)}</pre>\n"
            "<i>✅ chạy lại lượt này với full quyền · ♾ lưu rule vào settings của agy (áp dụng mọi dự án)</i>")
    await context.bot.send_message(chat_id, text[:4000], message_thread_id=thread_id, parse_mode=ParseMode.HTML,
                                   reply_markup=InlineKeyboardMarkup([buttons[:2], buttons[2:]]))


def agy_allow_rules(denied: list[dict]) -> list[str]:
    """Rule dạng command(<2 từ đầu>), ví dụ command(npm run)."""
    rules = []
    for d in denied:
        if d["kind"] == "command":
            words = d["target"].split()
            rule = f"command({' '.join(words[:2])})"
            if words and rule not in rules:
                rules.append(rule)
    return rules


def add_agy_allow_rules(rules: list[str]) -> list[str]:
    """Thêm rule vào permissions.allow của agy, giữ nguyên các khoá khác. Trả về rule mới thêm."""
    data = json.loads(AGY_SETTINGS.read_text()) if AGY_SETTINGS.exists() else {}
    allow = data.setdefault("permissions", {}).setdefault("allow", [])
    added = [r for r in rules if r not in allow]
    if added:
        allow.extend(added)
        tmp = AGY_SETTINGS.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(AGY_SETTINGS)
    return added


async def on_agy_permission(query, context, arg: str) -> None:
    token, _, choice = arg.partition(":")
    req = context.bot_data.get("agy_denied", {}).get(token)
    if not req:
        await query.answer("Yêu cầu này đã hết hạn.", show_alert=True)
        await query.edit_message_reply_markup(None)
        return
    key = req["key"]
    t = STATE["topics"].get(key)
    if choice != "skip" and (not t or engine_of(t) != "agy"):
        await query.answer("Topic không còn dùng Antigravity.", show_alert=True)
        return
    if choice != "skip" and key in RUNNING:
        await query.answer("Đang chạy, đợi xong rồi bấm lại.", show_alert=True)
        return
    context.bot_data["agy_denied"].pop(token, None)

    if choice == "skip":
        label = "❌ Đã bỏ qua"
    elif choice == "always":
        try:
            added = add_agy_allow_rules(agy_allow_rules(req["denied"]))
            label = "♾ Đã lưu rule: " + (", ".join(added) or "(đã có sẵn)")
        except (OSError, ValueError) as e:
            await query.answer(f"Không ghi được settings: {e}"[:190], show_alert=True)
            return
    else:
        label = "✅ Đã cho phép, chạy lại với full quyền"
    await query.answer(label[:190])
    original = query.message.text_html or ""
    try:
        await query.edit_message_text(f"{original}\n\n<b>{html.escape(label)}</b>"[:4096], parse_mode=ParseMode.HTML)
    except BadRequest:
        await query.edit_message_reply_markup(None)
    if choice == "skip":
        return

    listing = "\n".join(f"- {d['target']}" for d in req["denied"])
    prompt = ("Người dùng đã CHO PHÉP các thao tác bị chặn ở lượt trước. "
              f"Hãy thực hiện lại chúng và tiếp tục công việc:\n{listing}")
    await run_agy(context, req["chat_id"], req["thread_id"], key, t, prompt,
                  skip_permissions=(choice == "allow"))


async def set_commands(app: Application) -> None:
    """Đăng ký menu lệnh để gõ "/" là Telegram hiện gợi ý."""
    await app.bot.set_my_commands([
        BotCommand("new", "Tạo dự án mới"),
        BotCommand("open", "Chọn dự án có sẵn"),
        BotCommand("engine", "Chọn engine (Claude / Antigravity)"),
        BotCommand("mode", "Chọn chế độ quyền"),
        BotCommand("info", "Thông tin topic"),
        BotCommand("stop", "Dừng tác vụ đang chạy"),
        BotCommand("reset", "Bắt đầu phiên mới"),
        BotCommand("help", "Hướng dẫn"),
    ])


def main() -> None:
    if not ALLOWED_USERS:
        raise SystemExit("ALLOWED_USER_IDS trống: bắt buộc whitelist user ID của bạn.")
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(set_commands).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("engine", cmd_engine))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Projects root: %s | allowed users: %s", PROJECTS_ROOT, ALLOWED_USERS)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()

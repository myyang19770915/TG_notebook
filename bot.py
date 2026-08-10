#!/usr/bin/env python3
"""YangNBLM_bot — Telegram front-end for Google NotebookLM (Gemini Notebook).

Design:
  * Per-chat active notebook (SQLite), never relies on nblm's shared context.
  * Non-command text = ask the active notebook (the "免指令提問" feature).
  * Inline keyboard control panel for one-tap artifact generation.
  * Long generations are fire-and-forget; a JobQueue poller pushes the file
    back to the chat when the artifact completes.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import catalog
import nblm_client as nblm
import state

BASE = Path(__file__).parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)
UPLOADS = BASE / "uploads"
UPLOADS.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler(BASE / "bot.log"), logging.StreamHandler()],
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lm-bot")

TG_LIMIT = 3900
POLL_SECONDS = 45
JOB_TIMEOUT = 60 * 60  # 1h

ALLOWED: set[int] = set()
ALLOW_FIRST_USER = True  # 若白名單為空，第一位使用者自動綁定並寫回 .env


def _parse_allowlist(raw: str) -> set[int]:
    out: set[int] = set()
    for part in re.split(r"[,\s]+", raw or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def _persist_allowlist() -> None:
    """把目前白名單寫回 .env 的 ALLOWED_CHAT_IDS。"""
    envfile = BASE / ".env"
    value = ",".join(str(i) for i in sorted(ALLOWED))
    lines, found = [], False
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if line.startswith("ALLOWED_CHAT_IDS="):
                lines.append(f"ALLOWED_CHAT_IDS={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"ALLOWED_CHAT_IDS={value}")
    envfile.write_text("\n".join(lines) + "\n")
    os.chmod(envfile, 0o600)


def authorized(update: Update) -> bool:
    """白名單閘門。空白名單 + ALLOW_FIRST_USER → 首位使用者自動綁定。"""
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False
    uid, cid = user.id, chat.id

    if not ALLOWED and ALLOW_FIRST_USER:
        ALLOWED.add(uid)
        _persist_allowlist()
        log.warning("AUTO-BOUND first user id=%s (%s) — allowlist now %s",
                    uid, user.username or user.first_name, ALLOWED)
        return True

    if uid in ALLOWED or cid in ALLOWED:
        return True

    log.warning("DENIED user_id=%s chat_id=%s username=%s text=%r",
                uid, cid, user.username, (update.effective_message.text or "")[:80]
                if update.effective_message else "")
    return False


def load_env() -> None:
    envfile = BASE / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


# --------------------------------------------------------------------- utils

def esc(text: str) -> str:
    return html.escape(text or "")


async def send_long(msg_target, text: str, reply_markup=None, **kw):
    """Split >4096-char payloads on paragraph boundaries; markup on last chunk."""
    chunks: list[str] = []
    buf = ""
    for para in text.split("\n"):
        if len(buf) + len(para) + 1 > TG_LIMIT:
            if buf:
                chunks.append(buf)
            while len(para) > TG_LIMIT:
                chunks.append(para[:TG_LIMIT])
                para = para[TG_LIMIT:]
            buf = para
        else:
            buf = f"{buf}\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    chunks = chunks or [""]
    last = None
    for i, chunk in enumerate(chunks):
        mk = reply_markup if i == len(chunks) - 1 else None
        last = await msg_target.reply_text(chunk, reply_markup=mk, **kw)
    return last


def md_to_html(text: str) -> str:
    """Very small markdown->Telegram HTML converter (bold / code / headers)."""
    out = esc(text)
    out = re.sub(r"```(?:\w+)?\n(.*?)```", lambda m: f"<pre>{m.group(1)}</pre>", out, flags=re.S)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", out, flags=re.M)
    return out


def panel_markup(nb_title: str) -> InlineKeyboardMarkup:
    rows = []
    prim = catalog.PRIMARY
    for i in range(0, len(prim), 2):
        rows.append([
            InlineKeyboardButton(
                f"{catalog.EMOJI[k]} {catalog.CATALOG[k][1]}", callback_data=f"gen:{k}"
            )
            for k in prim[i:i + 2]
        ])
    rows.append([
        InlineKeyboardButton("📚 更多製品…", callback_data="menu:more"),
        InlineKeyboardButton("📦 已完成製品", callback_data="menu:artifacts"),
    ])
    rows.append([
        InlineKeyboardButton("➕ 加入來源", callback_data="menu:addsrc"),
        InlineKeyboardButton("🗂 來源管理", callback_data="menu:sources"),
    ])
    rows.append([
        InlineKeyboardButton("🔄 換筆記本", callback_data="menu:notebooks"),
        InlineKeyboardButton("❌ 清除選取", callback_data="menu:clear"),
    ])
    return InlineKeyboardMarkup(rows)


def more_markup() -> InlineKeyboardMarkup:
    rows = []
    sec = catalog.SECONDARY
    for i in range(0, len(sec), 2):
        rows.append([
            InlineKeyboardButton(
                f"{catalog.EMOJI[k]} {catalog.CATALOG[k][1]}", callback_data=f"gen:{k}"
            )
            for k in sec[i:i + 2]
        ])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="menu:panel")])
    return InlineKeyboardMarkup(rows)


def panel_text(title: str) -> str:
    return (
        f"📓 <b>當前操作筆記本：【{esc(title)}】</b>\n\n"
        "💬 <b>免指令直接提問</b>\n"
        "直接發送問題（不需加指令），系統會自動以此筆記本回答您。\n\n"
        "🛠 <b>一鍵生成 Studio 製品</b>\n"
        "點擊下方按鈕即可立即向雲端請求生成製品："
    )


# ------------------------------------------------------------------ commands

HELP = """👋 您好！我是 <b>NBLM 筆記本 BOT</b> - Google NotebookLM 智能助理。

現在我已支援 <b>互動控制面板</b> 與 <b>免指令提問</b>，可以更方便地與筆記本進行互動！

👉 <b>快速開始：</b>
1. 輸入 /notebooks 列出您的雲端筆記本。
2. 點擊按鈕選取筆記本，這會開啟<b>控制面板</b>，並將其設為「<b>作用中筆記本</b>」。
3. 之後您便可以<b>直接在對話中發送文字進行提問</b>，不需輸入任何指令！
4. 透過面板上的按鈕，也可以一鍵生成語音摘要、心智圖、資訊圖表等製品。

🛠 <b>手動指令（進階）：</b>
• /notebooks — 📓 列出筆記本並開啟面板
• /panel — 🎛 重新開啟目前面板
• /newnotebook &lt;標題&gt; — 🆕 建立新筆記本
• /add [內容] — ➕ 加入來源（網址／檔案／文字）
• /research &lt;主題&gt; — 🌐 上網研究並自動匯入來源
• /deepresearch &lt;主題&gt; — 🔬 深度研究（20+ 來源）
• /ask &lt;問題&gt; — 🔍 向作用中筆記本提問
• /sources — 📚 列出目前筆記本來源
• /artifacts — 📦 列出已生成製品
• /new — 🆕 開新對話（清除追問脈絡）
• /help — ❓ 顯示此說明訊息

📥 <b>加入來源的三種快捷方式：</b>
1. <b>直接貼網址</b>（含 YouTube）→ 自動加入為來源
2. <b>直接上傳檔案</b>（PDF/Word/音訊/影片/圖片，≤20MB）→ 自動加入
3. 點面板 ➕ 加入來源 → 選類型後貼內容

🎨 <b>製品一鍵生成手動指令（非必要）：</b>
格式：<code>/指令 [對焦主題] [風格設定]</code>
• /audio（語音摘要）| /slides（簡報）| /video（影片）
• /mindmap（心智圖）| /report（報告）| /quiz（測驗）
• /flashcards（學習卡）| /infographic（資訊圖表）| /datatable（資料表）

👉 範例：<code>/infographic QA 繁體中文/手繪</code>
👉 風格詞：繁體中文、英文、手繪、專業、便當、雜誌、直式、橫式、深入、簡短…"""


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)
    await cmd_notebooks(update, ctx)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_notebooks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    target = update.effective_message
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        nbs = await nblm.list_notebooks()
    except nblm.NblmError as e:
        return await target.reply_text(f"⚠️ {e}")
    if not nbs:
        return await target.reply_text("找不到任何筆記本。")

    ctx.chat_data["nb_cache"] = {nb["id"][:8]: (nb["id"], nb["title"]) for nb in nbs}
    rows = [
        [InlineKeyboardButton(f"📓 {nb['title'][:55]}", callback_data=f"nb:{nb['id'][:8]}")]
        for nb in nbs[:40]
    ]
    await target.reply_text(
        "📓 <b>請選擇要操作的筆記本：</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    row = state.get_active(update.effective_chat.id)
    if not row:
        return await cmd_notebooks(update, ctx)
    await update.effective_message.reply_text(
        panel_text(row["notebook_title"]),
        parse_mode=ParseMode.HTML,
        reply_markup=panel_markup(row["notebook_title"]),
    )


async def cmd_sources(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    row = state.get_active(update.effective_chat.id)
    if not row:
        return await update.effective_message.reply_text("尚未選取筆記本，請先 /notebooks")
    await _show_sources(update.effective_message, ctx, row)


async def _show_sources(target, ctx, row, manage: bool = False):
    try:
        srcs = await nblm.list_sources(row["notebook_id"])
    except nblm.NblmError as e:
        return await target.reply_text(f"⚠️ {e}")
    if not srcs:
        return await target.reply_text(
            "此筆記本尚無來源。點擊 ➕ 加入來源，或直接貼網址給我。",
            reply_markup=addsrc_markup())

    ctx.chat_data["src_cache"] = {s.get("id", "")[:8]: s for s in srcs}
    ready = sum(1 for s in srcs if (s.get("status") or "").lower() == "ready")
    lines = [f"📚 <b>{esc(row['notebook_title'])}</b> 共 {len(srcs)} 個來源"
             f"（就緒 {ready}）：\n"]
    for i, s in enumerate(srcs, 1):
        st = (s.get("status") or "").lower()
        mark = "✅" if st == "ready" else ("❌" if st == "error" else "⏳")
        lines.append(f"{i}. {mark} {esc(s.get('title', '(未命名)'))}")

    kb = None
    if manage:
        rows = [[InlineKeyboardButton(
            f"🗑 刪除：{(s.get('title') or '')[:40]}",
            callback_data=f"delsrc:{s.get('id', '')[:8]}")] for s in srcs[:20]]
        rows.append([InlineKeyboardButton("➕ 加入來源", callback_data="menu:addsrc")])
        rows.append([InlineKeyboardButton("⬅️ 返回面板", callback_data="menu:panel_msg")])
        kb = InlineKeyboardMarkup(rows)
    await send_long(target, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_artifacts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    row = state.get_active(update.effective_chat.id)
    if not row:
        return await update.effective_message.reply_text("尚未選取筆記本，請先 /notebooks")
    await _show_artifacts(update.effective_message, ctx, row)


async def _show_artifacts(target, ctx, row):
    try:
        arts = await nblm.list_artifacts(row["notebook_id"])
    except nblm.NblmError as e:
        return await target.reply_text(f"⚠️ {e}")
    if not arts:
        return await target.reply_text("此筆記本尚無製品。")
    ctx.chat_data["art_cache"] = {a.id[:8]: a for a in arts}
    rows, lines = [], ["📦 <b>已生成製品：</b>\n"]
    for a in arts[:30]:
        icon = {"completed": "✅", "in_progress": "⏳", "pending": "⏳"}.get(a.status, "❔")
        lines.append(f"{icon} {esc(a.title or a.type)} <i>({esc(a.type)})</i>")
        if a.status == "completed":
            rows.append([
                InlineKeyboardButton(f"⬇️ {(a.title or a.type)[:45]}", callback_data=f"dl:{a.id[:8]}")
            ])
    await target.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state.set_conversation(update.effective_chat.id, None)
    await update.message.reply_text("🆕 已開啟新對話，追問脈絡已清除。")


# ------------------------------------------------------------------- asking

async def do_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE, question: str):
    chat_id = update.effective_chat.id
    row = state.get_active(chat_id)
    if not row:
        return await update.effective_message.reply_text(
            "尚未選取作用中筆記本。請先輸入 /notebooks 並點選一本。"
        )
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        res = await nblm.ask(row["notebook_id"], question, row["conversation_id"])
    except nblm.NblmError as e:
        return await update.effective_message.reply_text(f"⚠️ {e}")

    if res.get("conversation_id"):
        state.set_conversation(chat_id, res["conversation_id"])
    answer = res.get("answer") or "(無回答)"
    refs = res.get("references") or []
    body = f"📖 <b>NotebookLM 回答（{esc(row['notebook_title'])}）：</b>\n\n{md_to_html(answer)}"
    if refs:
        body += f"\n\n<i>— 引用 {len(refs)} 處來源片段</i>"
    await send_long(update.effective_message, body, parse_mode=ParseMode.HTML)


async def cmd_ask(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args or []).strip()
    if not q:
        return await update.message.reply_text("用法：/ask 你的問題")
    await do_ask(update, ctx, q)


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """免指令提問；若處於「加入來源」模式則改為吸收來源。"""
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id

    mode = ctx.chat_data.pop(ADD_MODE_KEY, None)
    if mode == "url":
        return await ingest_text_source(update.message, ctx, chat_id, text)
    if mode == "text":
        return await ingest_text_source(update.message, ctx, chat_id, text,
                                        force_type="text")
    if mode == "research_fast":
        return await do_research(update.message, ctx, chat_id, text, "fast")
    if mode == "research_deep":
        return await do_research(update.message, ctx, chat_id, text, "deep")
    if mode == "file":
        await update.message.reply_text("（等待檔案上傳中，已收到文字則轉為提問）")

    # 裸貼網址 → 智慧判定為「加入來源」而非提問
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines and all(URL_RE.match(ln) for ln in lines):
        return await ingest_text_source(update.message, ctx, chat_id, text)

    await do_ask(update, ctx, text)


# --------------------------------------------------------------- add sources

URL_RE = re.compile(r"^https?://\S+$", re.I)

ADD_MODE_KEY = "add_mode"  # chat_data flag: 使用者處於「加入來源」模式


def addsrc_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 貼網址 / YouTube", callback_data="add:url")],
        [InlineKeyboardButton("📄 上傳檔案（PDF/Word/音訊…）", callback_data="add:file")],
        [InlineKeyboardButton("📝 貼上文字內容", callback_data="add:text")],
        [InlineKeyboardButton("🌐 網路研究（快速）", callback_data="add:research_fast")],
        [InlineKeyboardButton("🔬 網路研究（深度・較慢）", callback_data="add:research_deep")],
        [InlineKeyboardButton("⬅️ 返回面板", callback_data="menu:panel_msg")],
    ])


ADD_PROMPTS = {
    "url": "🔗 請直接貼上網址（支援一般網頁、YouTube、Google Docs）。\n可一次貼多個，每行一個。",
    "file": "📄 請直接上傳檔案（PDF、Word、TXT、Markdown、音訊、影片、圖片皆可）。",
    "text": "📝 請貼上要加入的文字內容，我會存成一則來源。",
    "research_fast": "🌐 請輸入研究主題，我會上網搜尋並自動匯入來源（約 1-2 分鐘）。",
    "research_deep": "🔬 請輸入研究主題，我會執行深度研究（20+ 來源，約 15-30 分鐘）。",
}


async def cmd_addsource(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    row = state.get_active(update.effective_chat.id)
    if not row:
        return await update.effective_message.reply_text("尚未選取筆記本，請先 /notebooks")
    raw = " ".join(ctx.args or []).strip() if hasattr(ctx, "args") and ctx.args else ""
    if raw:
        return await ingest_text_source(update.effective_message, ctx,
                                        update.effective_chat.id, raw)
    await update.effective_message.reply_text(
        f"➕ <b>加入來源到【{esc(row['notebook_title'])}】</b>\n請選擇來源類型：",
        parse_mode=ParseMode.HTML, reply_markup=addsrc_markup())


async def cmd_newnotebook(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    title = " ".join(ctx.args or []).strip()
    if not title:
        return await update.message.reply_text("用法：/newnotebook 筆記本標題")
    await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
    try:
        res = await nblm.create_notebook(title)
    except nblm.NblmError as e:
        return await update.message.reply_text(f"⚠️ 建立失敗：{e}")
    nb_id = res.get("id", "")
    state.set_active(update.effective_chat.id, nb_id, title)
    await update.message.reply_text(
        f"🆕 已建立筆記本【{esc(title)}】並設為作用中。\n"
        f"接著用 ➕ 加入來源，或直接貼網址給我。",
        parse_mode=ParseMode.HTML, reply_markup=panel_markup(title))


async def ingest_text_source(target, ctx, chat_id: int, text: str,
                             force_type: str | None = None):
    """把一段文字視為來源：可能是一或多個 URL，或純文字。"""
    row = state.get_active(chat_id)
    if not row:
        return await target.reply_text("尚未選取筆記本，請先 /notebooks")

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    urls = [ln for ln in lines if URL_RE.match(ln)]

    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)

    # 情境 A：一批網址
    if urls and force_type != "text" and len(urls) == len(lines):
        status = await target.reply_text(f"🔗 正在加入 {len(urls)} 個網址來源…")
        ok, fail = [], []
        for u in urls:
            try:
                res = await nblm.add_source(row["notebook_id"], u, type_="url")
                ok.append(res.get("title") or u)
            except nblm.NblmError as e:
                fail.append(f"{u} — {e}")
        msg = [f"✅ 已加入 {len(ok)} 個來源："]
        msg += [f"• {esc(t)}" for t in ok[:15]]
        if fail:
            msg.append(f"\n⚠️ 失敗 {len(fail)} 個：")
            msg += [f"• {esc(f)}" for f in fail[:5]]
        msg.append("\n<i>來源需索引 30 秒-數分鐘後才能提問，可用 /sources 確認狀態。</i>")
        return await status.edit_text("\n".join(msg), parse_mode=ParseMode.HTML)

    # 情境 B：純文字
    status = await target.reply_text("📝 正在加入文字來源…")
    title = (lines[0][:60] if lines else "文字來源")
    try:
        res = await nblm.add_source(row["notebook_id"], text, type_="text", title=title)
    except nblm.NblmError as e:
        return await status.edit_text(f"⚠️ 加入失敗：{esc(str(e))}", parse_mode=ParseMode.HTML)
    return await status.edit_text(
        f"✅ 已加入文字來源：<b>{esc(res.get('title') or title)}</b>",
        parse_mode=ParseMode.HTML)


async def do_research(target, ctx, chat_id: int, query: str, mode: str):
    row = state.get_active(chat_id)
    if not row:
        return await target.reply_text("尚未選取筆記本，請先 /notebooks")
    eta = "1-2 分鐘" if mode == "fast" else "15-30 分鐘"
    status = await target.reply_text(
        f"{'🌐' if mode == 'fast' else '🔬'} 已啟動<b>{'快速' if mode == 'fast' else '深度'}網路研究</b>："
        f"<code>{esc(query)}</code>\n預估 {eta}，完成後會自動匯入來源。",
        parse_mode=ParseMode.HTML)
    before = len(await _safe_sources(row["notebook_id"]))
    try:
        await nblm.add_research(row["notebook_id"], query, mode)
    except nblm.NblmError as e:
        return await status.edit_text(f"⚠️ 研究失敗：{esc(str(e))}", parse_mode=ParseMode.HTML)
    after = await _safe_sources(row["notebook_id"])
    gained = len(after) - before
    await status.edit_text(
        f"✅ 研究完成，新增約 <b>{max(gained, 0)}</b> 個來源"
        f"（目前共 {len(after)} 個）。\n可用 /sources 查看。",
        parse_mode=ParseMode.HTML)


async def _safe_sources(nb_id: str) -> list:
    try:
        return await nblm.list_sources(nb_id)
    except nblm.NblmError:
        return []


async def cmd_research(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args or []).strip()
    if not q:
        return await update.message.reply_text("用法：/research 研究主題")
    await do_research(update.message, ctx, update.effective_chat.id, q, "fast")


async def cmd_deepresearch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = " ".join(ctx.args or []).strip()
    if not q:
        return await update.message.reply_text("用法：/deepresearch 研究主題")
    await do_research(update.message, ctx, update.effective_chat.id, q, "deep")


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """檔案上傳 → 下載到本機 → nblm source add。"""
    chat_id = update.effective_chat.id
    row = state.get_active(chat_id)
    if not row:
        return await update.message.reply_text("尚未選取筆記本，請先 /notebooks")

    msg = update.message
    tg_file = msg.document or msg.audio or msg.video or msg.voice
    fname = None
    if msg.document:
        fname = msg.document.file_name
    elif msg.photo:
        tg_file = msg.photo[-1]
        fname = f"photo_{tg_file.file_unique_id}.jpg"
    if not tg_file:
        return

    size = getattr(tg_file, "file_size", 0) or 0
    if size > 20 * 1024 * 1024:
        return await msg.reply_text(
            "⚠️ Telegram Bot 僅能下載 20MB 以內的檔案。\n"
            "較大的檔案請先放到雲端並貼網址給我。")

    fname = fname or f"upload_{tg_file.file_unique_id}"
    dest = UPLOADS / re.sub(r"[^\w\u4e00-\u9fff.\-]+", "_", fname)[:80]
    status = await msg.reply_text(f"📄 正在上傳「{esc(fname)}」到筆記本…",
                                  parse_mode=ParseMode.HTML)
    try:
        f = await ctx.bot.get_file(tg_file.file_id)
        await f.download_to_drive(custom_path=str(dest))
    except Exception as e:  # noqa: BLE001
        log.exception("download from telegram failed")
        return await status.edit_text(f"⚠️ 取得檔案失敗：{e}")

    try:
        res = await nblm.add_source(row["notebook_id"], str(dest), type_="file")
    except nblm.NblmError as e:
        return await status.edit_text(f"⚠️ 加入來源失敗：{esc(str(e))}",
                                      parse_mode=ParseMode.HTML)
    await status.edit_text(
        f"✅ 已加入檔案來源：<b>{esc(res.get('title') or fname)}</b>\n"
        f"<i>索引需 30 秒-數分鐘，可用 /sources 確認。</i>",
        parse_mode=ParseMode.HTML)


# --------------------------------------------------------------- generation

async def start_generation(target, ctx, chat_id: int, kind: str, raw: str = ""):
    row = state.get_active(chat_id)
    if not row:
        return await target.reply_text("尚未選取筆記本，請先 /notebooks")

    gen_kind, zh, _, _, _, mins = catalog.CATALOG[kind]
    instructions, opts = catalog.parse_params(kind, raw)
    if kind == "datatable" and not instructions:
        instructions = "整理此筆記本的重點資料表"

    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    detail = "、".join(f"{k}={v}" for k, v in opts.items()) or "預設"
    status = await target.reply_text(
        f"{catalog.EMOJI[kind]} 已送出<b>{zh}</b>生成請求…\n"
        f"設定：<code>{esc(detail)}</code>\n"
        f"預估約 {mins} 分鐘，完成後我會主動推送檔案。",
        parse_mode=ParseMode.HTML,
    )

    # 記錄生成前的製品 ID，供無 task_id 時兜底比對
    before_ids = {a.id for a in await _safe_artifacts(row["notebook_id"])}

    try:
        res = await nblm.generate(row["notebook_id"], gen_kind, instructions, **opts)
    except nblm.NblmError as e:
        return await status.edit_text(f"⚠️ <b>{zh}</b> 生成失敗：{esc(str(e))}",
                                      parse_mode=ParseMode.HTML)

    # 情境 1：同步製品（mind-map）—— generate 當下就回完整內容，無 artifact_id
    if nblm.is_sync_payload(res):
        return await _deliver_sync(ctx, chat_id, status, kind, zh, res,
                                   row["notebook_title"])

    art_id = nblm.extract_artifact_id(res)

    # 情境 2：非同步但沒回 ID —— 比對前後差異找出新製品
    if not art_id:
        await asyncio.sleep(8)
        after = await _safe_artifacts(row["notebook_id"])
        new = [a for a in after if a.id not in before_ids]
        if new:
            art_id = new[0].id
            log.info("artifact id recovered by diff: %s", art_id)

    if not art_id:
        return await status.edit_text(
            f"⚠️ <b>{zh}</b> 已送出但未取得任務 ID，請稍後用 /artifacts 查看。\n"
            f"<i>回傳鍵：{esc(', '.join(list(res.keys())[:8]))}</i>",
            parse_mode=ParseMode.HTML)

    state.add_job(art_id, chat_id, row["notebook_id"], kind, zh, status.message_id)
    log.info("job queued kind=%s artifact=%s chat=%s", kind, art_id, chat_id)
    ctx.job_queue.run_once(poll_once, 5, data={"artifact_id": art_id, "chat_id": chat_id})


async def _safe_artifacts(nb_id: str) -> list:
    try:
        return await nblm.list_artifacts(nb_id)
    except nblm.NblmError:
        return []


async def _deliver_sync(ctx, chat_id, status, kind, zh, res, nb_title):
    """同步製品（心智圖）：直接把 JSON 存檔送出，並附文字大綱。"""
    payload = res.get("mind_map") or res.get("mindMap") or res
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", nb_title)[:40] or kind
    dest = DOWNLOADS / f"{safe}_心智圖_{int(time.time())}.json"
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    outline = _mindmap_outline(payload)
    try:
        await status.edit_text(f"✅ <b>{zh}</b> 生成完成。", parse_mode=ParseMode.HTML)
    except BadRequest:
        pass
    if outline:
        await send_long(status, f"🧠 <b>{esc(nb_title)} — 心智圖大綱</b>\n\n{esc(outline)}",
                        parse_mode=ParseMode.HTML)
    with dest.open("rb") as fh:
        await ctx.bot.send_document(chat_id, fh,
                                    caption=f"🧠 <b>{esc(nb_title)}</b>（心智圖 JSON）",
                                    parse_mode=ParseMode.HTML)


def _mindmap_outline(node, depth: int = 0, lines: list | None = None) -> str:
    """把心智圖樹壓成縮排文字大綱（最多 3 層、120 行）。"""
    if lines is None:
        lines = []
    if not isinstance(node, dict) or len(lines) > 120 or depth > 3:
        return "\n".join(lines)
    name = node.get("name") or node.get("title")
    if name:
        lines.append(("　" * depth) + ("• " if depth else "◆ ") + str(name))
    for child in (node.get("children") or []):
        _mindmap_outline(child, depth + 1, lines)
    return "\n".join(lines)


def make_gen_cmd(kind: str):
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        raw = " ".join(ctx.args or [])
        await start_generation(update.effective_message, ctx, update.effective_chat.id, kind, raw)
    handler.__name__ = f"cmd_gen_{kind}"
    return handler


# ------------------------------------------------------------------ callbacks

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    chat_id = q.message.chat_id
    data = q.data or ""
    await q.answer()

    if data.startswith("nb:"):
        cache = ctx.chat_data.get("nb_cache", {})
        entry = cache.get(data[3:])
        if not entry:
            return await q.message.reply_text("選項已過期，請重新 /notebooks")
        nb_id, title = entry
        state.set_active(chat_id, nb_id, title)
        return await q.message.reply_text(
            panel_text(title), parse_mode=ParseMode.HTML, reply_markup=panel_markup(title)
        )

    if data.startswith("add:"):
        kind = data[4:]
        ctx.chat_data[ADD_MODE_KEY] = kind
        return await q.message.reply_text(ADD_PROMPTS.get(kind, "請提供內容。"))

    if data == "menu:addsrc":
        row = state.get_active(chat_id)
        if not row:
            return await q.message.reply_text("尚未選取筆記本，請先 /notebooks")
        return await q.message.reply_text(
            f"➕ <b>加入來源到【{esc(row['notebook_title'])}】</b>\n請選擇來源類型：",
            parse_mode=ParseMode.HTML, reply_markup=addsrc_markup())

    if data == "menu:sources":
        row = state.get_active(chat_id)
        if not row:
            return await q.message.reply_text("尚未選取筆記本，請先 /notebooks")
        return await _show_sources(q.message, ctx, row, manage=True)

    if data == "menu:panel_msg":
        row = state.get_active(chat_id)
        if row:
            ctx.chat_data.pop(ADD_MODE_KEY, None)
            return await q.message.reply_text(
                panel_text(row["notebook_title"]), parse_mode=ParseMode.HTML,
                reply_markup=panel_markup(row["notebook_title"]))
        return

    if data.startswith("delsrc:"):
        row = state.get_active(chat_id)
        src = (ctx.chat_data.get("src_cache") or {}).get(data[7:])
        if not row or not src:
            return await q.message.reply_text("項目已過期，請重新開啟來源管理。")
        try:
            await nblm.delete_source(row["notebook_id"], src["id"])
        except nblm.NblmError as e:
            return await q.message.reply_text(f"⚠️ 刪除失敗：{e}")
        return await q.message.reply_text(
            f"🗑 已刪除來源：<b>{esc(src.get('title', ''))}</b>", parse_mode=ParseMode.HTML)

    if data.startswith("gen:"):
        return await start_generation(q.message, ctx, chat_id, data[4:])

    if data == "menu:more":
        try:
            return await q.edit_message_reply_markup(reply_markup=more_markup())
        except BadRequest:
            return

    if data == "menu:panel":
        row = state.get_active(chat_id)
        if row:
            try:
                return await q.edit_message_reply_markup(
                    reply_markup=panel_markup(row["notebook_title"]))
            except BadRequest:
                return
        return

    if data == "menu:notebooks":
        return await cmd_notebooks(update, ctx)

    if data == "menu:artifacts":
        row = state.get_active(chat_id)
        if row:
            return await _show_artifacts(q.message, ctx, row)
        return

    if data == "menu:clear":
        state.clear_active(chat_id)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except BadRequest:
            pass
        return await q.message.reply_text("❌ 已清除選取。輸入 /notebooks 重新挑選。")

    if data.startswith("dl:"):
        art = (ctx.chat_data.get("art_cache") or {}).get(data[3:])
        row = state.get_active(chat_id)
        if not art or not row:
            return await q.message.reply_text("項目已過期，請重新 /artifacts")
        return await deliver(ctx, chat_id, row["notebook_id"], art.id,
                             _kind_from_type(art.type, art.title), art.title or art.type)


_EXT_KIND = {
    ".csv": "datatable", ".mp3": "audio", ".mp4": "video", ".png": "infographic",
    ".pdf": "slides", ".pptx": "slides", ".json": "mindmap", ".md": "report",
}


def _kind_from_type(type_str: str, title: str = "") -> str:
    t = (type_str or "").lower()
    if "unknown" in t or not t:
        low = (title or "").lower()
        for ext, kind in _EXT_KIND.items():
            if low.endswith(ext):
                return kind
    table = [
        ("audio", "audio"), ("podcast", "audio"), ("video", "video"),
        ("infographic", "infographic"), ("mind", "mindmap"), ("slide", "slides"),
        ("deck", "slides"), ("quiz", "quiz"), ("flash", "flashcards"),
        ("table", "datatable"), ("report", "report"), ("brief", "report"),
        ("study", "report"), ("blog", "report"),
    ]
    for needle, kind in table:
        if needle in t:
            return kind
    return "report"


# ------------------------------------------------------------------ delivery

async def deliver(ctx, chat_id: int, notebook_id: str, artifact_id: str,
                  kind: str, label: str) -> bool:
    _, zh, dl_kind, ext, fmt, _ = catalog.CATALOG.get(kind, catalog.CATALOG["report"])
    safe = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", label)[:50] or kind
    dest = DOWNLOADS / f"{safe}_{artifact_id[:8]}{ext}"
    try:
        await nblm.download(notebook_id, dl_kind, artifact_id, str(dest), fmt)
    except nblm.NblmError as e:
        await ctx.bot.send_message(chat_id, f"⚠️ 下載「{label}」失敗：{e}")
        return False
    if not dest.exists() or dest.stat().st_size == 0:
        await ctx.bot.send_message(chat_id, f"⚠️ 「{label}」下載後檔案為空。")
        return False

    caption = f"{catalog.EMOJI.get(kind, '📎')} <b>{esc(label)}</b>（{zh}）"
    try:
        with dest.open("rb") as fh:
            if ext == ".png":
                await ctx.bot.send_photo(chat_id, fh, caption=caption,
                                         parse_mode=ParseMode.HTML)
            elif ext == ".mp3":
                await ctx.bot.send_audio(chat_id, fh, caption=caption,
                                         parse_mode=ParseMode.HTML)
            elif ext == ".mp4":
                await ctx.bot.send_video(chat_id, fh, caption=caption,
                                         parse_mode=ParseMode.HTML)
            else:
                await ctx.bot.send_document(chat_id, fh, caption=caption,
                                            parse_mode=ParseMode.HTML)
    except Exception as e:  # noqa: BLE001
        log.exception("send failed")
        await ctx.bot.send_message(chat_id, f"⚠️ 檔案已下載至 {dest}，但傳送失敗：{e}")
        return False
    return True


# ------------------------------------------------------------------- poller

async def poll_once(ctx: ContextTypes.DEFAULT_TYPE):
    """Poll a single job soon after creation (mind-map completes instantly)."""
    d = ctx.job.data
    await _check_job_by_id(ctx, d["artifact_id"], d["chat_id"])


async def poll_jobs(ctx: ContextTypes.DEFAULT_TYPE):
    for job in state.pending_jobs():
        await _check_job_by_id(ctx, job["artifact_id"], job["chat_id"], job)


async def _check_job_by_id(ctx, artifact_id: str, chat_id: int, job=None):
    if job is None:
        job = next((j for j in state.pending_jobs()
                    if j["artifact_id"] == artifact_id and j["chat_id"] == chat_id), None)
        if job is None:
            return
    age = time.time() - job["created_at"]
    try:
        status = await nblm.artifact_status(job["notebook_id"], artifact_id)
    except nblm.NblmError as e:
        log.warning("poll failed: %s", e)
        return

    if status == "completed":
        state.finish_job(artifact_id, chat_id)
        ok = await deliver(ctx, chat_id, job["notebook_id"], artifact_id,
                           job["kind"], job["label"])
        if ok and job["status_msg_id"]:
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id, message_id=job["status_msg_id"],
                    text=f"✅ <b>{esc(job['label'])}</b> 生成完成。",
                    parse_mode=ParseMode.HTML)
            except BadRequest:
                pass
    elif age > JOB_TIMEOUT:
        state.finish_job(artifact_id, chat_id)
        await ctx.bot.send_message(
            chat_id,
            f"⏰ <b>{esc(job['label'])}</b> 超過 1 小時仍未完成，已停止追蹤。"
            f"\n可稍後用 /artifacts 查看。",
            parse_mode=ParseMode.HTML)


# --------------------------------------------------------------------- error

async def gatekeeper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Runs in group -1 before every handler; stops unauthorized updates."""
    if authorized(update):
        return
    try:
        if update.callback_query:
            await update.callback_query.answer("⛔️ 未授權", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                "⛔️ 此為私人 Bot，您未在授權名單中。"
            )
    except Exception:  # noqa: BLE001
        pass
    raise ApplicationHandlerStop


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.exception("handler error", exc_info=ctx.error)


# ---------------------------------------------------------------------- main

async def post_init(app: Application):
    cmds = [
        BotCommand("notebooks", "📓 列出筆記本並開啟面板"),
        BotCommand("panel", "🎛 重新開啟控制面板"),
        BotCommand("add", "➕ 加入來源（網址/檔案/文字）"),
        BotCommand("research", "🌐 網路研究並匯入來源"),
        BotCommand("deepresearch", "🔬 深度網路研究"),
        BotCommand("newnotebook", "🆕 建立新筆記本"),
        BotCommand("ask", "🔍 向作用中筆記本提問"),
        BotCommand("sources", "📚 列出來源"),
        BotCommand("artifacts", "📦 已生成製品"),
        BotCommand("new", "🆕 開新對話"),
        BotCommand("audio", "🎧 語音摘要"),
        BotCommand("infographic", "📊 資訊圖表"),
        BotCommand("mindmap", "🧠 心智圖"),
        BotCommand("slides", "📽 簡報"),
        BotCommand("report", "📄 報告"),
        BotCommand("quiz", "📝 測驗"),
        BotCommand("flashcards", "🃏 學習卡"),
        BotCommand("datatable", "📋 資料表"),
        BotCommand("video", "🎬 影片"),
        BotCommand("help", "❓ 說明"),
    ]
    await app.bot.set_my_commands(cmds)
    log.info("bot commands registered")


def main() -> None:
    load_env()
    state.init()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_BOT_TOKEN 未設定")

    ALLOWED.update(_parse_allowlist(os.environ.get("ALLOWED_CHAT_IDS", "")))
    if ALLOWED:
        log.info("白名單啟用：%s", sorted(ALLOWED))
    else:
        log.warning("白名單為空 — 第一位互動的使用者將被自動綁定為擁有者")

    app = Application.builder().token(token).post_init(post_init).build()

    # 白名單閘門必須在所有 handler 之前
    app.add_handler(TypeHandler(Update, gatekeeper), group=-1)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("notebooks", cmd_notebooks))
    app.add_handler(CommandHandler("panel", cmd_panel))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("artifacts", cmd_artifacts))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("addsource", cmd_addsource))
    app.add_handler(CommandHandler("add", cmd_addsource))
    app.add_handler(CommandHandler("newnotebook", cmd_newnotebook))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("deepresearch", cmd_deepresearch))

    for kind in catalog.CATALOG:
        app.add_handler(CommandHandler(kind, make_gen_cmd(kind)))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.AUDIO | filters.VIDEO | filters.VOICE
        | filters.PHOTO, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.job_queue.run_repeating(poll_jobs, interval=POLL_SECONDS, first=20)

    log.info("LM bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

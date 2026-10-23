"""Slothbox Telegram Bot Implementation (Long Polling).
Provides Universal Retrieval, Local RAG, Interactive Buttons, and Rich Onboarding.
"""

import asyncio
import io
import logging
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.ai import AIEngine, OllamaProcessingError
from src.config import Settings, get_settings
from src.db import Database
from src.ingest import detect_content_type, process_item_pipeline, save_media_file

logger = logging.getLogger("slothbox")


def authorized_only(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator to enforce single-user authorization based on TELEGRAM_ALLOWED_USER_ID."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any) -> Any:
        user = update.effective_user
        settings: Settings = context.bot_data["settings"]

        if not user or user.id != settings.telegram_allowed_user_id:
            logger.warning(
                f"Unauthorized access attempt from user_id={user.id if user else 'Unknown'}, "
                f"username={user.username if user else 'None'}"
            )
            if update.message:
                await update.message.reply_text("⛔ Access Denied. This Slothbox instance is private.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Access Denied.", show_alert=True)
            return None

        return await func(update, context, *args, **kwargs)
    return wrapper


# -----------------------------------------------------------------------------
# Universal Retrieval Engine
# -----------------------------------------------------------------------------

async def retrieve_and_send_item(
    chat_id: int,
    item_id: int,
    bot: Any,
    db: Database,
    settings: Settings,
) -> bool:
    """
    Retrieve and send back the original content, photo, document, or clickable link card.
    Guarantees 100% reclaimability of anything stored in Slothbox.
    """
    item = db.get_item(item_id)
    if not item:
        await bot.send_message(chat_id=chat_id, text=f"Item #{item_id} not found in database.")
        return False

    content_type = item["content_type"]
    raw_content = item["raw_content"]
    file_path = item.get("file_path")
    summary = item.get("summary") or "No summary available."
    tags_str = " ".join([f"#{t}" for t in item.get("tags", [])])

    # 1. Image File Retrieval
    if content_type == "image":
        if file_path and Path(file_path).exists():
            caption = f"📸 *Item #{item_id} (Original Image)*\n\n*Summary:* {summary}\n{tags_str}"
            with open(file_path, "rb") as f:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=caption[:1024],
                    parse_mode=ParseMode.MARKDOWN,
                )
            return True
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📸 Item #{item_id} (Image record):\nImage file was not found on disk at {file_path}.",
            )
            return False

    # 2. Document File Retrieval
    elif content_type == "document":
        if file_path and Path(file_path).exists():
            caption = f"📄 *Item #{item_id} (Original Document)*\n\n*Summary:* {summary}\n{tags_str}"
            with open(file_path, "rb") as f:
                await bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=caption[:1024],
                    parse_mode=ParseMode.MARKDOWN,
                )
            return True
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=f"📄 Item #{item_id} (Document record):\nFile was not found on disk at {file_path}.",
            )
            return False

    # 3. Web URL / Link Retrieval
    elif content_type == "url":
        buttons = [
            [
                InlineKeyboardButton("🌐 Open in Browser", url=raw_content),
                InlineKeyboardButton("📖 Reader Mode", callback_data=f"read:{item_id}"),
            ],
            [
                InlineKeyboardButton("🗄️ Archive", callback_data=f"archive:{item_id}"),
            ],
        ]
        if settings.elevenlabs_api_key:
            buttons[1].append(InlineKeyboardButton("🎙️ Voice", callback_data=f"voice:{item_id}"))

        text = (
            f"🔗 *Item #{item_id} (Web Link)*\n\n"
            f"• *URL:* {raw_content}\n\n"
            f"• *Summary:* {summary}\n\n"
            f"• *Tags:* {tags_str}"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True

    # 4. Text Note Retrieval
    else:
        buttons = [
            [
                InlineKeyboardButton("🗄️ Archive", callback_data=f"archive:{item_id}"),
            ]
        ]
        if settings.elevenlabs_api_key:
            buttons[0].append(InlineKeyboardButton("🎙️ Voice", callback_data=f"voice:{item_id}"))

        text = (
            f"📝 *Item #{item_id} (Original Note)*\n\n"
            f"{raw_content}\n\n"
            f"─────────────────\n"
            f"*Summary:* {summary}\n"
            f"*Tags:* {tags_str}"
        )
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN,
        )
        return True


# -----------------------------------------------------------------------------
# Background Pipeline Dispatcher with Interactive Actions
# -----------------------------------------------------------------------------

async def _run_background_pipeline(
    item_id: int,
    chat_id: int,
    bot: Any,
    db: Database,
    ai_engine: AIEngine,
    settings: Settings,
) -> None:
    """Execute background processing and notify the user with interactive action buttons."""
    try:
        success, message, _ = await process_item_pipeline(item_id, db, ai_engine)
        if success:
            item = db.get_item(item_id)
            c_type = item["content_type"] if item else "item"

            get_label = (
                "📥 Open Link" if c_type == "url"
                else ("📥 Get Image" if c_type == "image"
                else ("📥 Get Document" if c_type == "document"
                else "📥 Get Note"))
            )
            buttons = [
                [
                    InlineKeyboardButton(get_label, callback_data=f"get:{item_id}"),
                    InlineKeyboardButton("🗄️ Archive", callback_data=f"archive:{item_id}"),
                ]
            ]
            if c_type == "url":
                buttons[0].insert(1, InlineKeyboardButton("📖 Read", callback_data=f"read:{item_id}"))

            if settings.elevenlabs_api_key:
                buttons.append([InlineKeyboardButton("🎙️ Listen (Voice)", callback_data=f"voice:{item_id}")])

            await bot.send_message(
                chat_id=chat_id,
                text=message,
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        else:
            await bot.send_message(chat_id=chat_id, text=message)
    except Exception as exc:
        logger.error(f"Error in background task for item #{item_id}: {exc}", exc_info=True)
        await bot.send_message(
            chat_id=chat_id,
            text=f"Slothbox couldn't process item #{item_id}: Internal error: {exc}",
        )


# -----------------------------------------------------------------------------
# Onboarding & Help Directory
# -----------------------------------------------------------------------------

def _build_guide_text() -> str:
    """Constructs comprehensive onboarding and command guide for /start and /help."""
    return (
        "🦥 *Welcome to Slothbox — Your Private Second Brain*\n"
        "_Save it. Forget it. Find it._\n\n"
        "Slothbox is a privacy-first personal knowledge vault. All AI processing runs "
        "**100% locally on your machine** via Ollama. All data lives in your private local SQLite database on disk. "
        "Zero cloud leakage, zero fake data.\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "📥 *How to Store Anything:*\n"
        "Simply forward or send anything directly to this chat:\n"
        "• 📝 *Text Notes:* Send thoughts, ideas, meeting notes, code snippets\n"
        "• 🔗 *Web Links:* Send any URL (auto-scraped, summarized & archived)\n"
        "• 📸 *Images & Photos:* Send screenshots, diagrams, posters (analyzed via local vision AI)\n"
        "• 📄 *Documents:* Send PDFs, markdown, or text files (stored & indexed)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛠️ *Command Directory & Short Descriptions:*\n\n"
        "🔍 *Recall & Search:*\n"
        "• `/search <query>` — Semantic vector search across your notes, links & images\n"
        "• `/ask <question>` — 🧠 *Local RAG* AI answers questions synthesized from your stored notes\n"
        "• `/get <id>` — 📥 Retrieve original photo, document, link card, or clean note\n"
        "• `/read <id>` — 📖 Read clean, distraction-free article text directly in Telegram\n\n"
        "🗂️ *Vault & Stash Management:*\n"
        "• `/recent` — List last 5 saved items with 1-tap interactive action buttons\n"
        "• `/stats` — Vault statistics, processed item breakdown, and top tags\n"
        "• `/save <text>` — Manually save a text note (same as sending directly)\n\n"
        "🔄 *Triage & Resurfacing:*\n"
        "• `/digest` — Resurface a random forgotten note saved >7 days ago\n"
        "• `/keep <id>` — Retain a resurfaced item in your active rotation\n"
        "• `/archive <id>` — Archive an item out of active search & digest queries\n\n"
        "🎙️ *Audio & Voice:*\n"
        "• `/voice <id>` — Generate a realistic AI voice audio summary (ElevenLabs)\n\n"
        "ℹ️ *Onboarding & Help:*\n"
        "• `/start` — Application overview, features & interactive menu\n"
        "• `/help` — Full command directory and usage instructions\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *Pro Tips:*\n"
        "• Every saved item and search result includes 1-tap action buttons (📥 Get, 📖 Read, 🗄️ Archive, 🎙️ Voice).\n"
        "• Send photos with a custom caption: Slothbox fuses your manual thoughts with AI vision!\n"
        "• Tap any of the quick-action buttons below to explore your vault:"
    )


def _build_guide_keyboard() -> InlineKeyboardMarkup:
    """Interactive quick-action keyboard for /start and /help."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 View Stats", callback_data="ui:stats"),
            InlineKeyboardButton("🕒 Recent Items", callback_data="ui:recent"),
        ],
        [
            InlineKeyboardButton("💡 Daily Digest", callback_data="ui:digest"),
            InlineKeyboardButton("❓ Need Help?", callback_data="ui:help_prompt"),
        ],
    ])


@authorized_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command with full guide and interactive navigation."""
    if update.message:
        await update.message.reply_text(
            _build_guide_text(),
            reply_markup=_build_guide_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )


@authorized_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command with full directory and quick buttons."""
    if update.message:
        await update.message.reply_text(
            _build_guide_text(),
            reply_markup=_build_guide_keyboard(),
            parse_mode=ParseMode.MARKDOWN,
        )


# -----------------------------------------------------------------------------
# Retrieval & Reading Commands
# -----------------------------------------------------------------------------

@authorized_only
async def get_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /get <id> command to retrieve original file, photo, text, or link."""
    if not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/get <item_id>` (e.g., `/get 4`)", parse_mode=ParseMode.MARKDOWN)
        return

    item_id = int(context.args[0])
    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]

    await retrieve_and_send_item(
        chat_id=update.effective_chat.id,  # type: ignore
        item_id=item_id,
        bot=context.bot,
        db=db,
        settings=settings,
    )


@authorized_only
async def read_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /read <id> command for clean article reader mode."""
    if not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/read <item_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    item_id = int(context.args[0])
    db: Database = context.bot_data["db"]
    item = db.get_item(item_id)
    if not item:
        await update.message.reply_text(f"Item #{item_id} not found.")
        return

    full_text = item.get("full_text") or item.get("raw_content") or ""
    title_summary = item.get("summary") or "Clean Reader View"

    header = f"📖 *Reader View: Item #{item_id}*\n*Summary:* {title_summary}\n\n─────────────────\n"
    readable = full_text[:3800] if len(full_text) > 3800 else full_text

    buttons = [
        [InlineKeyboardButton("📥 Get Original", callback_data=f"get:{item_id}")]
    ]
    if item["content_type"] == "url":
        buttons[0].append(InlineKeyboardButton("🌐 Open Web", url=item["raw_content"]))

    await update.message.reply_text(
        header + readable,
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ask <question> Local RAG reasoning command."""
    if not update.message:
        return

    question = " ".join(context.args).strip() if context.args else ""
    if not question:
        await update.message.reply_text(
            "Please ask a question about your saved notes:\n`/ask <your question>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    db: Database = context.bot_data["db"]
    ai_engine: AIEngine = context.bot_data["ai_engine"]

    stored_embeddings = db.get_all_embeddings()
    if not stored_embeddings:
        await update.message.reply_text("Your stash is empty. Save some notes first to ask questions!")
        return

    await update.message.reply_text("🧠 *Consulting your second brain...*", parse_mode=ParseMode.MARKDOWN)

    try:
        q_vector = await ai_engine.generate_embedding(question)
        matches = ai_engine.compute_top_similar(q_vector, stored_embeddings, top_k=3, min_score=0.15)
    except Exception as exc:
        await update.message.reply_text(f"Search error while answering: {exc}")
        return

    if not matches:
        await update.message.reply_text(f"I couldn't find any relevant items in your stash about: *{question}*", parse_mode=ParseMode.MARKDOWN)
        return

    context_items = []
    retrieval_buttons = []
    for item_id, score in matches:
        it = db.get_item(item_id)
        if it:
            context_items.append(it)
            retrieval_buttons.append(InlineKeyboardButton(f"📥 #{item_id}", callback_data=f"get:{item_id}"))

    try:
        answer = await ai_engine.answer_question(question, context_items)
        keyboard = InlineKeyboardMarkup([retrieval_buttons]) if retrieval_buttons else None
        try:
            await update.message.reply_text(
                f"🧠 *Slothbox Answer:*\n\n{answer}",
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            # Fallback without markdown parsing if LLM output contained unescaped brackets/underscores
            await update.message.reply_text(
                f"🧠 Slothbox Answer:\n\n{answer}",
                reply_markup=keyboard,
            )
    except Exception as exc:
        await update.message.reply_text(f"Error during reasoning: {exc}")


@authorized_only
async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /voice <id> command to generate ElevenLabs speech audio."""
    if not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/voice <item_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    item_id = int(context.args[0])
    await _generate_and_send_voice(chat_id=update.effective_chat.id, item_id=item_id, context=context)  # type: ignore


async def _generate_and_send_voice(chat_id: int, item_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper to generate ElevenLabs audio for an item and send as voice note."""
    settings: Settings = context.bot_data["settings"]
    if not settings.elevenlabs_api_key:
        await context.bot.send_message(
            chat_id=chat_id,
            text="ElevenLabs API key is not configured in .env. Voice synthesis is unavailable.",
        )
        return

    db: Database = context.bot_data["db"]
    ai_engine: AIEngine = context.bot_data["ai_engine"]
    item = db.get_item(item_id)
    if not item:
        await context.bot.send_message(chat_id=chat_id, text=f"Item #{item_id} not found.")
        return

    summary = item.get("summary") or item.get("raw_content") or ""
    speech_text = f"Here is your summary for item number {item_id}. {summary}"

    await context.bot.send_message(chat_id=chat_id, text="🎙️ *Generating voice audio...*", parse_mode=ParseMode.MARKDOWN)

    audio_bytes = await ai_engine.text_to_speech(speech_text, settings.elevenlabs_api_key)
    if not audio_bytes:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Failed to generate voice audio for item #{item_id}. Please check ElevenLabs permissions.",
        )
        return

    voice_file = io.BytesIO(audio_bytes)
    voice_file.name = f"slothbox_item_{item_id}.mp3"
    await context.bot.send_voice(
        chat_id=chat_id,
        voice=voice_file,
        caption=f"🎙️ Audio Summary: Item #{item_id}",
    )


# -----------------------------------------------------------------------------
# Stash Management Commands
# -----------------------------------------------------------------------------

@authorized_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats command."""
    db: Database = context.bot_data["db"]
    stats = db.get_stats()

    if stats["total_items"] == 0:
        if update.message:
            await update.message.reply_text("Slothbox has 0 items. Send me notes or links to start stashing!")
        return

    types_str = ", ".join([f"{k}: {v}" for k, v in stats["by_type"].items()]) if stats["by_type"] else "None"
    
    if stats["top_tags"]:
        top_tags_str = ", ".join([f"#{t} ({cnt})" for t, cnt in stats["top_tags"].items()])
    else:
        top_tags_str = "No tags yet"

    message = (
        f"📊 *Slothbox Stash Stats*\n\n"
        f"• *Total items:* {stats['total_items']}\n"
        f"• *Processed:* {stats['processed']}\n"
        f"• *Pending:* {stats['pending']}\n"
        f"• *Failed:* {stats['failed']}\n"
        f"• *Archived:* {stats['archived']}\n\n"
        f"• *Content Types:* {types_str}\n"
        f"• *Top Tags:* {top_tags_str}"
    )
    if update.message:
        await update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def recent_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /recent command with one-tap interactive retrieval buttons."""
    db: Database = context.bot_data["db"]
    recent = db.get_recent_items(limit=5)

    if not recent:
        if update.message:
            await update.message.reply_text("Your Slothbox stash is currently empty.")
        return

    lines = ["🕒 *Recent Stash Items:*\n"]
    action_buttons = []

    for item in recent:
        status_icon = "✅" if item["status"] == "processed" else ("⏳" if item["status"] == "pending" else "❌")
        summary = item["summary"] or item["raw_content"][:80].replace("\n", " ")
        tags_str = " ".join([f"#{t}" for t in item["tags"]]) if item["tags"] else ""
        lines.append(f"{status_icon} *#{item['id']}* [{item['content_type']}] {summary}\n{tags_str}")
        action_buttons.append(
            InlineKeyboardButton(f"📥 #{item['id']} Get", callback_data=f"get:{item['id']}")
        )

    keyboard = [action_buttons[i : i + 2] for i in range(0, len(action_buttons), 2)]

    if update.message:
        await update.message.reply_text(
            "\n\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN,
        )


@authorized_only
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /search command with interactive retrieval action buttons."""
    if not update.message:
        return

    query = " ".join(context.args).strip() if context.args else ""
    if not query:
        await update.message.reply_text("Please provide a query: `/search <what you are looking for>`", parse_mode=ParseMode.MARKDOWN)
        return

    db: Database = context.bot_data["db"]
    ai_engine: AIEngine = context.bot_data["ai_engine"]

    stored_embeddings = db.get_all_embeddings()
    if not stored_embeddings:
        await update.message.reply_text("No processed items with embeddings in your stash yet.")
        return

    try:
        query_vector = await ai_engine.generate_embedding(query)
    except OllamaProcessingError as err:
        logger.warning(f"Embedding search error: {err}")
        await update.message.reply_text("Slothbox search failed: embedding model unavailable.")
        return
    except Exception as err:
        logger.error(f"Search exception: {err}", exc_info=True)
        await update.message.reply_text(f"Slothbox search failed: {err}")
        return

    matches = ai_engine.compute_top_similar(query_vector, stored_embeddings, top_k=3, min_score=0.15)
    if not matches:
        await update.message.reply_text(f"No close matches found for: *{query}*", parse_mode=ParseMode.MARKDOWN)
        return

    results = [f"🔍 *Top results for:* \"{query}\"\n"]
    button_rows = []

    for rank, (item_id, score) in enumerate(matches, 1):
        item = db.get_item(item_id)
        if not item:
            continue
        summary = item["summary"] or item["raw_content"][:100]
        tags = " ".join([f"#{t}" for t in item["tags"]])
        date_str = item["created_at"][:10] if item.get("created_at") else ""
        results.append(
            f"{rank}. *#{item['id']}* (Similarity: {int(score * 100)}%)\n"
            f"   {summary}\n"
            f"   {tags} | {date_str}"
        )
        button_rows.append([
            InlineKeyboardButton(f"📥 #{item['id']} Get", callback_data=f"get:{item['id']}"),
            InlineKeyboardButton(f"🗄️ #{item['id']} Archive", callback_data=f"archive:{item['id']}"),
        ])

    await update.message.reply_text(
        "\n\n".join(results),
        reply_markup=InlineKeyboardMarkup(button_rows),
        parse_mode=ParseMode.MARKDOWN,
    )


@authorized_only
async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /digest resurfacing command."""
    if not update.message:
        return

    db: Database = context.bot_data["db"]
    candidates = db.get_resurface_candidates(days=7, limit=1)

    if not candidates:
        await update.message.reply_text("No items to resurface.")
        return

    item = candidates[0]
    item_id = item["id"]
    summary = item["summary"] or item["raw_content"][:150]
    tags_str = " ".join([f"#{t}" for t in item["tags"]])

    try:
        created_dt = datetime.strptime(item["created_at"], "%Y-%m-%d %H:%M:%S")
        days_ago = (datetime.now(timezone.utc) - created_dt).days
    except Exception:
        days_ago = 7

    text = (
        f"🦥 *Resurfaced Item #{item_id}*\n"
        f"You saved this {days_ago} days ago. Still interested?\n\n"
        f"{summary}\n\n"
        f"Tags: {tags_str}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Get Item", callback_data=f"get:{item_id}"),
            InlineKeyboardButton("Keep", callback_data=f"keep:{item_id}"),
            InlineKeyboardButton("Archive", callback_data=f"archive:{item_id}"),
        ]
    ])

    await update.message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)


@authorized_only
async def keep_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle explicit /keep <id> command."""
    if not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/keep <item_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    item_id = int(context.args[0])
    db: Database = context.bot_data["db"]
    if db.mark_item_kept(item_id):
        await update.message.reply_text(f"Kept item #{item_id} in your active stash.")
    else:
        await update.message.reply_text(f"Item #{item_id} not found.")


@authorized_only
async def archive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle explicit /archive <id> command."""
    if not update.message:
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: `/archive <item_id>`", parse_mode=ParseMode.MARKDOWN)
        return

    item_id = int(context.args[0])
    db: Database = context.bot_data["db"]
    if db.mark_item_archived(item_id):
        await update.message.reply_text(f"Archived item #{item_id}. It will not appear in search or daily digest.")
    else:
        await update.message.reply_text(f"Item #{item_id} not found.")


# -----------------------------------------------------------------------------
# Callback Query Handler (Interactive Buttons)
# -----------------------------------------------------------------------------

@authorized_only
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Universal handler for inline button actions (get, read, voice, keep, archive, ui)."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    data = query.data
    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]
    chat_id = update.effective_chat.id  # type: ignore

    # UI Quick actions from /start and /help
    if data.startswith("ui:"):
        ui_action = data.split("ui:")[1]
        if ui_action == "stats":
            stats = db.get_stats()
            if stats["total_items"] == 0:
                await context.bot.send_message(chat_id=chat_id, text="Slothbox has 0 items. Send notes or links to get started!")
            else:
                types_str = ", ".join([f"{k}: {v}" for k, v in stats["by_type"].items()]) if stats["by_type"] else "None"
                top_tags_str = ", ".join([f"#{t} ({cnt})" for t, cnt in stats["top_tags"].items()]) if stats["top_tags"] else "No tags"
                msg = f"📊 *Stash Stats:*\n\n• *Total items:* {stats['total_items']}\n• *Processed:* {stats['processed']}\n• *Types:* {types_str}\n• *Tags:* {top_tags_str}"
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN)
        elif ui_action == "recent":
            recent = db.get_recent_items(limit=5)
            if not recent:
                await context.bot.send_message(chat_id=chat_id, text="Your stash is empty.")
            else:
                lines = ["🕒 *Recent Items:*\n"]
                buttons = []
                for it in recent:
                    lines.append(f"• *#{it['id']}* [{it['content_type']}] {it['summary'] or it['raw_content'][:60]}")
                    buttons.append(InlineKeyboardButton(f"📥 #{it['id']}", callback_data=f"get:{it['id']}"))
                keyboard = [buttons[i:i+2] for i in range(0, len(buttons), 2)]
                await context.bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        elif ui_action == "digest":
            candidates = db.get_resurface_candidates(days=7, limit=1)
            if not candidates:
                await context.bot.send_message(chat_id=chat_id, text="No items older than 7 days to resurface.")
            else:
                item = candidates[0]
                await context.bot.send_message(chat_id=chat_id, text=f"🦥 *Resurfaced Item #{item['id']}*\n{item['summary'] or item['raw_content'][:100]}", parse_mode=ParseMode.MARKDOWN)
        elif ui_action == "help_prompt":
            await context.bot.send_message(
                chat_id=chat_id,
                text=_build_guide_text(),
                reply_markup=_build_guide_keyboard(),
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    # Item Actions
    action, _, item_id_str = data.partition(":")
    if not item_id_str.isdigit():
        return

    item_id = int(item_id_str)

    if action == "get":
        await retrieve_and_send_item(
            chat_id=chat_id,
            item_id=item_id,
            bot=context.bot,
            db=db,
            settings=settings,
        )
    elif action == "read":
        item = db.get_item(item_id)
        if item:
            full_text = item.get("full_text") or item.get("raw_content") or ""
            readable = full_text[:3800] if len(full_text) > 3800 else full_text
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"📖 *Reader Mode: #{item_id}*\n\n{readable}",
                parse_mode=ParseMode.MARKDOWN,
            )
    elif action == "voice":
        await _generate_and_send_voice(chat_id=chat_id, item_id=item_id, context=context)
    elif action == "keep":
        db.mark_item_kept(item_id)
        if query.message:
            await query.edit_message_text(f"{query.message.text}\n\n*Status:* Kept in active stash.", parse_mode=ParseMode.MARKDOWN)
    elif action == "archive":
        db.mark_item_archived(item_id)
        if query.message:
            await query.edit_message_text(f"{query.message.text}\n\n*Status:* Archived from active stash.", parse_mode=ParseMode.MARKDOWN)


# -----------------------------------------------------------------------------
# Ingestion Message Handlers
# -----------------------------------------------------------------------------

@authorized_only
async def save_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /save <text or url>."""
    if not update.message:
        return
    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await update.message.reply_text("Usage: `/save <text or URL>` (or just send the content directly).", parse_mode=ParseMode.MARKDOWN)
        return

    await _handle_text_ingest(text, update, context)


@authorized_only
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text and URL captures."""
    if not update.message or not update.message.text:
        return
    await _handle_text_ingest(update.message.text, update, context)


async def _handle_text_ingest(text: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Helper for ingesting text or URL."""
    db: Database = context.bot_data["db"]
    ai_engine: AIEngine = context.bot_data["ai_engine"]
    settings: Settings = context.bot_data["settings"]

    content_type = detect_content_type(text)
    item_id = db.insert_item(raw_content=text, content_type=content_type)

    if update.message:
        await update.message.reply_text("Saved to Slothbox. Processing...")

    asyncio.create_task(
        _run_background_pipeline(
            item_id=item_id,
            chat_id=update.effective_chat.id,  # type: ignore
            bot=context.bot,
            db=db,
            ai_engine=ai_engine,
            settings=settings,
        )
    )


@authorized_only
async def photo_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo/image capture with optional manual caption fusion."""
    if not update.message or not update.message.photo:
        return

    db: Database = context.bot_data["db"]
    ai_engine: AIEngine = context.bot_data["ai_engine"]
    settings: Settings = context.bot_data["settings"]

    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    file_bytes = await photo_file.download_as_bytearray()

    caption = update.message.caption or "Uploaded Image"
    saved_path = save_media_file(bytes(file_bytes), f"{photo.file_unique_id}.jpg", settings.media_dir)

    item_id = db.insert_item(
        raw_content=caption,
        content_type="image",
        file_path=str(saved_path),
    )

    await update.message.reply_text("Saved image to Slothbox. Processing...")

    asyncio.create_task(
        _run_background_pipeline(
            item_id=item_id,
            chat_id=update.effective_chat.id,  # type: ignore
            bot=context.bot,
            db=db,
            ai_engine=ai_engine,
            settings=settings,
        )
    )


@authorized_only
async def document_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle document capture with optional manual caption fusion."""
    if not update.message or not update.message.document:
        return

    db: Database = context.bot_data["db"]
    ai_engine: AIEngine = context.bot_data["ai_engine"]
    settings: Settings = context.bot_data["settings"]

    doc = update.message.document
    doc_file = await doc.get_file()
    file_bytes = await doc_file.download_as_bytearray()

    filename = doc.file_name or f"{doc.file_unique_id}.bin"
    saved_path = save_media_file(bytes(file_bytes), filename, settings.media_dir)

    caption = update.message.caption or f"Document: {filename}"
    item_id = db.insert_item(
        raw_content=caption,
        content_type="document",
        file_path=str(saved_path),
    )

    await update.message.reply_text(f"Saved document '{filename}' to Slothbox. Processing...")

    asyncio.create_task(
        _run_background_pipeline(
            item_id=item_id,
            chat_id=update.effective_chat.id,  # type: ignore
            bot=context.bot,
            db=db,
            ai_engine=ai_engine,
            settings=settings,
        )
    )


# -----------------------------------------------------------------------------
# Application Setup & Runner
# -----------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    """Register standard bot menu commands with descriptions in Telegram."""
    commands = [
        BotCommand("start", "Overview, app info & quick menu"),
        BotCommand("help", "All commands & usage instructions"),
        BotCommand("search", "Semantic search across your stash"),
        BotCommand("ask", "Ask questions about your notes (local RAG)"),
        BotCommand("get", "Retrieve original photo, doc, link, or note"),
        BotCommand("read", "Read clean, distraction-free article text"),
        BotCommand("recent", "View last 5 saved items with action buttons"),
        BotCommand("stats", "Vault statistics & top tags breakdown"),
        BotCommand("save", "Manually save a note or link"),
        BotCommand("digest", "Resurface forgotten notes older than 7 days"),
        BotCommand("keep", "Keep a resurfaced item active in your vault"),
        BotCommand("archive", "Archive an item from active search & digest"),
        BotCommand("voice", "Generate AI voice audio memo (ElevenLabs)"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Successfully registered bot menu commands with Telegram.")
    except Exception as exc:
        logger.warning(f"Could not register Telegram bot menu commands: {exc}")


def build_application(settings: Settings) -> Application:
    """Build and configure the python-telegram-bot Application instance."""
    db = Database(settings.sqlite_db_path)
    ai_engine = AIEngine(
        base_url=settings.ollama_base_url,
        llm_model=settings.ollama_llm_model,
        embed_model=settings.ollama_embed_model,
        vision_model=settings.ollama_vision_model,
        rag_model=settings.ollama_rag_model,
    )

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .build()
    )

    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["ai_engine"] = ai_engine

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("save", save_command))
    application.add_handler(CommandHandler("get", get_command))
    application.add_handler(CommandHandler("read", read_command))
    application.add_handler(CommandHandler("ask", ask_command))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("recent", recent_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("digest", digest_command))
    application.add_handler(CommandHandler("keep", keep_command))
    application.add_handler(CommandHandler("archive", archive_command))
    application.add_handler(CommandHandler("stats", stats_command))

    # Register callback query handler for all interactive buttons
    application.add_handler(CallbackQueryHandler(button_callback))

    # Register content capture message handlers
    application.add_handler(MessageHandler(filters.PHOTO, photo_message_handler))
    application.add_handler(MessageHandler(filters.Document.ALL, document_message_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    return application


def main() -> None:
    """Entry point for running Slothbox via Long Polling."""
    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.INFO,
    )
    logger.info("Starting Slothbox Telegram Bot...")

    try:
        settings = get_settings()
    except Exception as exc:
        logger.error(f"Failed to load configuration: {exc}")
        return

    app = build_application(settings)
    logger.info(f"Slothbox configured. Whitelisted User ID: {settings.telegram_allowed_user_id}")
    logger.info("Starting long polling (start_polling)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()

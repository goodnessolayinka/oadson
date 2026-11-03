# Telegram Skill

Send messages, files, and photos to OGA's Telegram via @Oadsonbot.
Also receive messages sent to the bot.

## When to use this skill

Use `@telegram` when OGA wants to:
- Send himself a message on Telegram
- Deliver a generated file to his Telegram
- Check messages sent to @Oadsonbot
- Get notified about something on his phone

## Actions

| Action | When to use |
|--------|-------------|
| send_message | Send text to OGA's Telegram |
| send_file | Send a document/PDF/ZIP to Telegram |
| send_photo | Send an image to Telegram |
| get_updates | Read recent messages sent to the bot |
| get_chat_id | First-time setup — get OGA's chat ID |
| set_webhook | Register Railway URL for incoming messages |
| delete_webhook | Unregister the current webhook — use before switching deploy targets or to stop incoming-message delivery temporarily |

## Examples

"Send me a Telegram saying the file is ready"
→ action: send_message, text: "Your file is ready"

"Send the PDF to my Telegram"
→ action: send_file, file_url: <url>, filename: "report.pdf"

"Check my Telegram messages"
→ action: get_updates

## Setup required

Environment variables on Railway:
- TELEGRAM_BOT_TOKEN — from @BotFather
- TELEGRAM_OWNER_CHAT_ID — your personal chat ID (get via get_chat_id action)

First time:
1. Open Telegram → @Oadsonbot → tap Start
2. Ask OADSON: "@telegram get my chat ID"
3. Add the returned ID to Railway as TELEGRAM_OWNER_CHAT_ID
4. Ask OADSON: "@telegram set webhook https://oadsonv2-production.up.railway.app"

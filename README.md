# AutoBot

Telegram channel join-request automation bot with admin controls and feature-styled messages.

## Render environment variables

- `BOT_TOKEN`: Telegram bot token.
- `MONGODB_URI`: MongoDB Atlas connection URI.
- `MONGODB_DB`: MongoDB database name (defaults to `joinbot`).
- `ADMIN_IDS` and optionally `ADMIN_IDS_2`, `ADMIN_IDS_3`, `ADMIN_ID`: comma-separated Telegram user IDs allowed to use administrative commands.

## Commands

- `/start`: shows the built-in welcome card or the custom message configured by an admin.
- `/setstart`: configure the public start message (text or supported media).
- `/default`: restore the built-in welcome card.
- `/joinmessage`: save a text or supported media message to send after successful join-request approval.
- `/empty`: disable the saved join message.
- `/broadcast`: copy one incoming message to saved user IDs, preserving supported original content and inline keyboard.
- `/stats`: show user counts, join-request counters, broadcast delivery counters, database state, and process uptime.
- `/cancel`: cancel an active setup or broadcast prompt.

Only configured admin IDs can use administrative commands. Anyone can use `/start`.

## Runtime and data

- The app runs as a Flask web service and receives Telegram updates through a secret-verified webhook.
- There is no application polling loop or background cleanup loop. Run one Render web service instance.
- MongoDB stores saved message configuration, aggregate counters, and user IDs. User records contain the Telegram user ID as `_id` and a `created_at` timestamp; a TTL index expires records after 365 days (TTL deletion can be delayed by MongoDB's monitor).
- Broadcast content is copied directly from the admin's source message and is not stored as a broadcast payload in MongoDB. Users must have started the bot before the bot can message them.
- Built-in bot replies use a consistent dark-neon-inspired text layout. Telegram's native chat-action status text and colours are controlled by Telegram and cannot be custom-themed. Admin-configured custom start/join messages retain their original content and formatting.
- Albums/grouped media are not guaranteed to be copied as a single album by the one-message broadcast flow.

## Health

The `/health` endpoint reports service, database, admin configuration, webhook mode, auto-approval capability, and user-ID retention configuration.

# JoinBot

Telegram join-request automation bot.

## Render environment variables

- `BOT_TOKEN`: Telegram bot token.
- `MONGODB_URI`: MongoDB Atlas connection URI (database path: `joinbot`).
- `MONGODB_DB`: MongoDB database name (`joinbot`).
- `ADMIN_IDS` and optionally `ADMIN_IDS_2`, `ADMIN_IDS_3`, `ADMIN_ID`: comma-separated Telegram user IDs allowed to use administrative commands.

## Commands

- `/start`: shows the default intro or the custom message set by an admin.
- `/setstart`: configure the public start message.
- `/default`: restore the built-in start message.
- `/joinmessage`: save a global message/media post to send after successful join-request approval.
- `/empty`: disable the saved join message only.
- `/stats`: show live counters and service uptime.
- `/cancel`: cancel an active message setup.

Only configured admin IDs can use administrative commands. Anyone can use `/start`.

The bot uses MongoDB for persistent configuration, counters, media storage, and single-poller coordination. Run exactly one Render instance.

"""Render entry point for JoinBot."""
from bot import app, start_bot
import os

if __name__ == "__main__":
    start_bot()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")), threaded=True)

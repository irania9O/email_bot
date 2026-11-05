# iust-mail

Polls the IUST Exchange inbox and forwards new emails to a Telegram chat.

    cp .env.example .env   # fill in values
    uv run iust-mail

On first run, existing emails are marked as seen; only mail arriving afterwards is forwarded.

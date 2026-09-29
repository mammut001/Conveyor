"""setup_wizard — interactive `conveyor setup` for secrets and integrations.

Secrets (bot tokens, API keys, mail passwords) are entered here, on the
server, never through chat. Each module asks, verifies live, and only then
writes `.env` (backup first, atomic replace, mode 600).

Run: ``python -m setup_wizard [module]`` or ``conveyor setup [module]``.
"""

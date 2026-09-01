"""Convenience entry point for the Remote Control Desk service."""

import asyncio

from control_bot.main import main


if __name__ == "__main__":
    asyncio.run(main())

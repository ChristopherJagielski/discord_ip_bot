import asyncio
import contextlib
import logging
import os
import signal

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("ip_bot")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
IPIFY_URL = "https://api.ipify.org"
# Channel to pin IP-change announcements to (falls back to the command's channel).
IP_CHANNEL_ID = os.getenv("IP_CHANNEL_ID")
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


def validate_config() -> None:
    """Fail fast on missing or malformed configuration."""
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is not set in .env")
    if IP_CHANNEL_ID:
        try:
            int(IP_CHANNEL_ID)
        except ValueError:
            raise SystemExit(f"IP_CHANNEL_ID must be a numeric channel ID, got: {IP_CHANNEL_ID!r}")


class IPBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.current_ip: str | None = None  # in-memory storage of the last known IP

    async def setup_hook(self) -> None:
        # Register the /currentip command.
        @self.tree.command(name="currentip", description="Show the bot's public IP address")
        async def currentip(interaction: discord.Interaction) -> None:
            await interaction.response.defer()

            try:
                async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                    async with session.get(IPIFY_URL) as resp:
                        resp.raise_for_status()
                        ip = (await resp.text()).strip()
            except Exception:
                await interaction.followup.send(
                    "Sorry, I couldn't fetch the public IP right now.", ephemeral=True
                )
                return

            changed = ip != self.current_ip
            self.current_ip = ip

            if changed:
                log.info("New public IP detected: %s", ip)
                # Announce the change in the pinned channel (or this channel).
                if IP_CHANNEL_ID:
                    try:
                        channel = await self.fetch_channel(int(IP_CHANNEL_ID))
                    except discord.NotFound:
                        log.warning("Configured channel %s not found, skipping pin.", IP_CHANNEL_ID)
                        channel = None
                else:
                    channel = interaction.channel
                if channel is not None and isinstance(channel, discord.abc.Messageable):
                    try:
                        msg = await channel.send(f"📌 Public IP changed: `{ip}`")
                        await msg.pin()
                    except discord.Forbidden:
                        log.warning("No permission to post/pin in the target channel.")
                    except discord.HTTPException:
                        # e.g. pinning is not possible in DMs
                        log.warning("Could not pin the announcement (DMs don't support pins).")
                else:
                    log.warning("Target channel not found, skipping pin.")

            await interaction.followup.send(f"Public IP: `{ip}`")

        # Sync global commands so /currentip appears in Discord.
        synced = await self.tree.sync()
        log.info("Synced %d global command(s): %s", len(synced), [c.name for c in synced])

    async def on_ready(self) -> None:
        """Called when the bot has logged in and received the READY event (also on reconnects)."""
        if self.user is None:
            log.warning("on_ready fired but self.user is None, skipping logs.")
            return
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        guilds = self.guilds
        log.info("Connected to %d guild(s):", len(guilds))
        for guild in guilds:
            # guild.approximate_member_count is the reliable total; len(guild.members)
            # only counts members present in the local cache.
            log.info("  - %s (ID: %s, ~%s members)", guild.name, guild.id, guild.approximate_member_count)
        log.info("Bot is ready.")

    async def on_close(self) -> None:
        """Called when the connection is closing — log a clean shutdown."""
        log.info("Bot is shutting down...")
        await super().on_close()


async def main() -> None:
    validate_config()
    log.info("Starting IP bot...")

    loop = asyncio.get_running_loop()
    stop = loop.create_future()

    def _request_shutdown(signame: str) -> None:
        log.info("Received %s, shutting down gracefully...", signame)
        if not stop.done():
            stop.set_result(None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _request_shutdown, sig.name)

    async with IPBot() as bot:
        started = asyncio.create_task(bot.start(TOKEN))
        done, _ = await asyncio.wait({started, stop}, return_when=asyncio.FIRST_COMPLETED)

        if stop in done:
            # Graceful: let discord.py close the websocket and clean up.
            bot.close()
            try:
                await asyncio.wait_for(started, timeout=10)
            except asyncio.TimeoutError:
                log.warning("Timed out waiting for clean disconnect, exiting anyway.")
                started.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await started
        else:
            # bot.start() returned on its own (e.g. login failure) — surface it.
            for task in done:
                task.result()


if __name__ == "__main__":
    asyncio.run(main())

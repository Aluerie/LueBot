from typing import TYPE_CHECKING

import twitchio
from discord.utils import cached_property
from twitchio.ext import commands

from bot import IrenesBot

from .. import const

__all__ = ("IrenesCog",)


class IrenesCog(commands.Cog):
    def __init__(self, bot: IrenesBot) -> None:
        self.bot: IrenesBot = bot

    # @property
    def irene_channel(self) -> twitchio.Channel:
        """Get Irene's channel from the cache"""
        channel_name = const.IRENE_TWITCH_NAME
        channel = self.bot.get_channel(channel_name)
        if channel:
            return channel
        else:
            msg = f"Channel name={channel_name} not in cache"
            raise RuntimeError(msg)

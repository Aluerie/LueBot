from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Sequence
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, override

import discord
from discord.ext import tasks
from discord.utils import MISSING

if TYPE_CHECKING:
    import datetime

    from ..bot import LueBot

    class HasBotAttribute(Protocol):
        bot: LueBot


log = logging.getLogger(__name__)

__all__ = ("lueloop",)


_func = Callable[..., Coroutine[Any, Any, Any]]
LF = TypeVar("LF", bound=_func)


class LueLoop(tasks.Loop[LF]):
    """My subclass for discord.ext.tasks.Loop.

    Just extra boilerplate functionality.

    Notes
    -----
    Sorry to twitchio guys, but at the moment of writing this `discord.ext.tasks` is way more fleshed out
    and offers a bit more functionality than `twitchio.ext.routines`.

    Warning
    -------
    The task should be initiated in a class that has `.bot` of LueBot type. Otherwise, it will just fail.
    All my tasks (and all my code is in cogs that do have `.bot` but still)

    """

    def __init__(
        self,
        coro: LF,
        seconds: float,
        hours: float,
        minutes: float,
        time: datetime.time | Sequence[datetime.time],
        count: int | None,
        reconnect: bool,
        name: str | None,
    ) -> None:
        super().__init__(coro, seconds, hours, minutes, time, count, reconnect, name)

    #     self._before_loop = self._base_before_loop

    # async def _base_before_loop(self, cog: HasBotAttribute) -> None:  # *args: Any
    #     """A standard coro to `_before_loop`.

    #     Otherwise every task has same
    #     ```py
    #     @my_task.before_loop
    #     @other_task.before_loop
    #     async def my_task_before(self):
    #         await self.bot.wait_until_ready()
    #     ```
    #     fragment of code.
    #     """
    #     await cog.bot.wait_for_ready()

    @override
    async def _error(self, cog: HasBotAttribute, exception: Exception) -> None:
        """Same `_error` as in parent class but with `exc_manager` integrated."""
        embed = (
            discord.Embed(title=self.coro.__name__, colour=0x1A7A8A)
            .set_author(name=f"{self.coro.__module__}: {self.coro.__qualname__}")
            .set_footer(text=f"{self.__class__.__name__}: {self.coro.__name__}")
        )
        await cog.bot.exc_manager.register_error(exception, embed)


@discord.utils.copy_doc(tasks.loop)
def lueloop(
    *,
    seconds: float = MISSING,
    minutes: float = MISSING,
    hours: float = MISSING,
    time: datetime.time | Sequence[datetime.time] = MISSING,
    count: int | None = None,
    reconnect: bool = True,
    name: str | None = None,
) -> Callable[[LF], LueLoop[LF]]:
    """Copy-pasted `loop` decorator from `discord.ext.tasks` corresponding to AluLoop class.

    Notes
    -----
    * if `discord.ext.tasks` gets extra cool features which will be represented in a change of `tasks.loop`
        decorator/signature we would need to manually update this function (or maybe even AluLoop class)

    """

    def decorator(func: LF) -> LueLoop[LF]:
        return LueLoop(
            func,
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            count=count,
            time=time,
            reconnect=reconnect,
            name=name,
        )

    return decorator

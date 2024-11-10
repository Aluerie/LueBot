from __future__ import annotations

import abc
import asyncio
import datetime
import itertools
import logging
from typing import TYPE_CHECKING, TypedDict, override

from steam import ID
from steam.ext.dota2 import GameMode, Hero, LobbyType
from thefuzz import process

from bot import irenes_loop
from utils import errors, formats

from .constants import HERO_ALIASES, PLAYER_COLOURS
from .enums import LobbyParam0, PlayerMatchOutcome, RPStatus, Team, WinLossCategory
from .utils import convert_id3_to_id64, rank_medal_display_name

if TYPE_CHECKING:
    from steam.ext.dota2 import MatchHistoryMatch, MatchMinimal

    from bot import IrenesBot

    type ActiveMatch = PlayMatch | WatchMatch

    class UpdateLastGamesQuery1Row(TypedDict):
        match_id: int
        hero_id: int

    class WLResponseQuery(TypedDict):
        game_mode: int
        lobby_type: int
        start_time: datetime.datetime
        outcome: int

    class MMRCommandQuery(TypedDict):
        mmr: int
        medal: str

    class FixMatchHistoryQuery1Row(TypedDict):
        match_id: int
        team: int


__all__ = ("Streamer",)

log = logging.getLogger(__name__)
log.setLevel(logging.DEBUG)


class Streamer:
    def __init__(self, bot: IrenesBot, steam_id64: int) -> None:
        self.bot: IrenesBot = bot

        self.steam_id64: int = steam_id64
        self.account_id: int = ID(steam_id64).id
        self.rich_presence: dict[str, str] | None = None
        self.rp_status: RPStatus = RPStatus.Offline
        self.play_match: PlayMatch | None = None
        self.watch_match: WatchMatch | None = None

        self.promised_match_ids: dict[int, Hero] = {}  # match_id -> hero
        self.last_game: LastGame | None = None

        self.match_history_ready: bool = False
        self.unsupported_error: str = ""

    @property
    def active_match(self) -> PlayMatch | WatchMatch | None:
        return self.play_match or self.watch_match

    def reset(self, event_msg: str, *, unsupported_error: str = "") -> None:
        log.debug("Resetting streamer state to play_match=`None`: %s", event_msg)
        if p := self.play_match:
            p.reset()
            self.bot.dispatch("reset_streamer", event_msg)
            if p.match_id and p.hero:
                self.promised_match_ids[p.match_id] = p.hero
                if not self.update_last_game.is_running():
                    self.update_last_game.start()

        elif w := self.watch_match:
            w.reset()
            self.bot.dispatch("reset_streamer", event_msg)

        self.play_match = None
        self.watch_match = None
        self.unsupported_error = unsupported_error

    async def update(self) -> None:
        user = self.bot.dota.get_user(self.steam_id64)
        if not user:
            try:
                log.debug("Streamer %s not in cache... fetching", self.account_id)
                async with asyncio.timeout(4.0):
                    user = await self.bot.dota.fetch_user(self.steam_id64)
            except TimeoutError:
                msg = "TimeoutError when fetching the user."
                raise errors.PlaceholderRaiseError(msg)

        # don't do anything if not needed.
        rich_presence = user.rich_presence

        if rich_presence is None:
            rp_status = RPStatus.Offline
            if self.rp_status != rp_status:
                self.rp_status = rp_status
                self.reset("Streamer went Offline")
                self.bot.dispatch("rich_presence_changed", self.rp_status.display_name)
            return

        # this will bite me back one day, but "param1" is a hero level
        # it's annoying that levelling up triggers the whole scheme below
        # so we just ignore this param1 all together.
        rich_presence.pop("param1", None)

        if self.rich_presence == rich_presence:
            # if (self.active_match is None) or (self.active_match.is_hero_ready and self.active_match.is_data_ready):
            # no changes in rich presence
            # or no need to fetch data from the match
            return
        else:
            # Detected Rich Presence change;
            self.rich_presence = rich_presence
            log.debug("Rich Presence = %s", rich_presence)

        rp_status = RPStatus.try_value(rich_presence.get("status") or "#MY_NoStatus")  # hard-coded

        if self.rp_status != rp_status:
            # Detected Rich Presence change;
            # only debug purpose since code still goes further
            self.bot.dispatch("rich_presence_changed", rp_status)
            log.debug("RPStatus changed to `%s`", rp_status.display_name)

        self.rp_status = rp_status

        match self.rp_status:
            case RPStatus.Idle | RPStatus.MainMenu | RPStatus.Finding:
                # Streamer is in Main Menu
                self.reset("Streamer in Main Menu")
            case RPStatus.HeroSelection | RPStatus.Strategy | RPStatus.Playing:
                # Streamer is in a match as a player
                watchable_game_id = rich_presence.get("WatchableGameID")
                watching_server = rich_presence.get("watching_server")

                if watchable_game_id is None:
                    # something is off
                    lobby_param0 = rich_presence.get("param0")

                    if lobby_param0 == LobbyParam0.DemoMode:
                        self.reset("DemoMode", unsupported_error="Demo Mode is not supported")
                    elif lobby_param0 == LobbyParam0.BotMatch:
                        self.reset("PrivateLobby", unsupported_error="Private Lobbies are not supported")
                    else:
                        msg = f'RP is "Playing" but {watchable_game_id=} and {lobby_param0=}'
                        raise errors.PlaceholderRaiseError(msg)
                elif watchable_game_id == "0":
                    # something is off again
                    party_state = rich_presence.get("party")
                    if party_state and "party_state: UI" in party_state:
                        # hacky but this is what happens when we quit the match into main manu sometimes
                        self.reset("Exited Match")
                    else:
                        msg = f'RP is "Playing" but {watchable_game_id=} and {party_state=}'
                        raise errors.PlaceholderRaiseError(msg)
                else:
                    # all is good
                    if not self.play_match:
                        # started playing new match
                        self.play_match = PlayMatch(self.bot, watchable_game_id, watching_server, self.account_id)
                    elif (
                        self.play_match.watchable_game_id != watchable_game_id
                        or self.play_match.watching_server != watching_server
                    ):
                        self.reset("Started new match bypassing Menu")
                        # started playing new match right so fast after bypassing Menu
                        self.play_match = PlayMatch(self.bot, watchable_game_id, watching_server, self.account_id)

            case RPStatus.Spectating:
                watching_server = rich_presence.get("watching_server")
                if watching_server is None:
                    msg = f"RP is Playing but {watching_server=}"
                    raise errors.PlaceholderRaiseError(msg)
                else:
                    self.watch_match = WatchMatch(self.bot, watching_server)
            case _:
                self.reset("Unknown to the bot Steam Status.")

    async def add_completed_match_to_database(
        self, history_match: MatchHistoryMatch
    ) -> tuple[MatchMinimal, PlayerMatchOutcome]:
        partial_match = self.bot.dota.instantiate_partial_match(history_match.id)
        minimal_match = await partial_match.minimal()

        outcome = PlayerMatchOutcome.create_from_history(minimal_match, history_match)
        query = """
            INSERT INTO ttv_dota_matches
            (match_id, account_id, hero_id, game_mode, lobby_type, start_time, outcome)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
        """
        await self.bot.pool.execute(
            query,
            history_match.id,
            self.account_id,
            history_match.hero.id,
            history_match.game_mode.value,
            history_match.lobby_type.value,
            history_match.start_time,
            outcome.value,
        )
        return minimal_match, outcome

    async def update_mmr(self, mmr_change: int) -> None:
        partial_user = self.bot.dota.instantiate_partial_user(self.account_id)
        profile_card = await partial_user.dota2_profile_card()
        medal = rank_medal_display_name(profile_card)
        query = """
            UPDATE ttv_dota_streamers
            SET mmr = mmr + $1, medal = $2
            WHERE account_id = $3
        """
        await self.bot.pool.execute(query, mmr_change, medal, self.account_id)

    @irenes_loop(seconds=20, count=30)
    async def update_last_game(self) -> None:
        mmr_change: int = 0

        partial_user = self.bot.dota.instantiate_partial_user(self.account_id)
        match_history = await partial_user.match_history()

        matches_to_pop = []
        for match_id, hero in self.promised_match_ids.items():
            history_match = next(iter(match for match in match_history if match.id == match_id), None)
            if history_match is None:
                continue
            try:
                minimal_match, outcome = await self.add_completed_match_to_database(history_match)
            except TimeoutError:
                continue
            last_game = LastGame(minimal_match, outcome, self.account_id, hero)
            # set last game
            if not self.last_game or last_game.match_id > self.last_game.match_id:
                self.last_game = last_game
            mmr_change += outcome.mmr_change(last_game.lobby_type)
            matches_to_pop.append(match_id)

        # 2. UPDATE MMR AND RANK_TIER
        await self.update_mmr(mmr_change)

        # 3. Clear promised matches
        for match_id in matches_to_pop:
            self.promised_match_ids.pop(match_id)

        if not self.promised_match_ids:
            self.update_last_game.stop()

    async def fix_match_history(self) -> None:
        """Cross-check matches between database and streamer's match history.

        This function
            * fixes the database by fixing existing matches and then adding recent matches into it.
            * adjusts mmr
            * fills in `.last_game` attribute
        """
        log.debug("`fix_match_history` is starting.")

        # 0. Delta mmr to apply in the end after all fixing
        mmr_change: int = 0

        # 1. Fetch last known match_id with a valid result.
        query = """
            SELECT match_id
            FROM ttv_dota_matches
            WHERE account_id = $1
            ORDER BY match_id DESC
            LIMIT 1
        """
        last_known_match_id: int | None = await self.bot.pool.fetchval(query, self.account_id)

        # 2. Fill in Last Game from Match History.
        partial_user = self.bot.dota.instantiate_partial_user(self.account_id)
        history_matches = await partial_user.match_history(start_at_match_id=0)
        latest_match = await history_matches[0].minimal()
        latest_outcome = PlayerMatchOutcome.create_from_history(latest_match, history_matches[0])

        self.last_game = LastGame(latest_match, latest_outcome, self.account_id, history_matches[0].hero)

        # 3. Look in match history to get unknown recently played games.
        cutoff_dt = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=48)
        while True:  # monkaGIGA
            for history_match in history_matches:
                if (
                    last_known_match_id and history_match.id <= last_known_match_id
                ) or history_match.start_time < cutoff_dt:
                    break
                _, outcome = await self.add_completed_match_to_database(history_match)
                mmr_change += outcome.mmr_change(history_match.lobby_type)
            else:  # only executed if the inner loop did NOT break, always forget...
                # need to queue more :o
                history_matches = await partial_user.match_history(start_at_match_id=history_matches[-1].id)
            break  # only executed if the inner loop DID break

        # 4. Finally make mmr changes into the database
        await self.update_mmr(mmr_change)

        self.match_history_ready = True
        log.debug("`match_history_ready` is ready.")

    async def wl_command_response(self) -> str:
        if not self.match_history_ready:
            return "Match history is not ready yet."

        query = """
            SELECT game_mode, lobby_type, outcome, start_time
            FROM ttv_dota_matches
            WHERE account_id = $1
            ORDER BY start_time DESC
        """
        db_games: list[WLResponseQuery] = await self.bot.pool.fetch(query, self.account_id)

        # find last gaming session
        mapping: dict[WinLossCategory, list[int]] = {}
        # this will be dict[WLCategory, [amount_of_losses, amount_of_wins]]
        # or better to say value[0] is losses, [1] is wins
        loop_dt = datetime.datetime.now(datetime.UTC)
        for game in db_games:
            if game["outcome"] not in (PlayerMatchOutcome.Win, PlayerMatchOutcome.Loss):
                continue
            if game["start_time"] < loop_dt - datetime.timedelta(hours=6):
                break
            else:
                # tha match is included into current gaming session
                loop_dt = game["start_time"]
                category = WinLossCategory.create(game["lobby_type"], game["game_mode"])
                mapping.setdefault(category, [0, 0])[int(game["outcome"])] += 1

        if not mapping:
            return "0 W - 0 L"
        return ", ".join(f"{category.name} {wl[1]} W - {wl[0]} L" for category, wl in mapping.items())

    async def mmr_command_response(self) -> str:
        query = "SELECT mmr, medal FROM ttv_dota_streamers WHERE account_id = $1"
        row: MMRCommandQuery = await self.bot.pool.fetchrow(query, self.account_id)
        return f"[Estimated] {row['mmr']} \N{BULLET} {row['medal']}"


class LastGame:
    def __init__(self, match: MatchMinimal, outcome: PlayerMatchOutcome, account_id: int, hero: Hero) -> None:
        # print(match.id, [player.hero for player in match.players])
        # find proper player
        streamer_player = next((player for player in match.players if player.id == account_id), None)
        if streamer_player is None:
            # attempt #2 (it should work for New Player Mode games)
            streamer_player = next((player for player in match.players if player.hero == hero), None)
            if streamer_player is None:
                msg = f"Somehow user {account_id} / hero {hero}  are not in the match {match.id}"
                raise errors.SomethingWentWrong(msg)

        self.players: dict[int, Hero] = {player.id: player.hero for player in match.players if player.id}
        self.match_id: int = match.id
        self.start_time: datetime.datetime = match.start_time
        self.ended_dt: datetime.datetime = match.start_time + match.duration
        self.kda: str = f"{streamer_player.kills}/{streamer_player.deaths}/{streamer_player.assists}"
        self.outcome: PlayerMatchOutcome = outcome
        self.lobby_type: LobbyType = match.lobby_type
        self.game_mode: GameMode = match.game_mode
        self.hero: Hero = streamer_player.hero
        self.wl_category: WinLossCategory = WinLossCategory.create(match.lobby_type, match.game_mode)

    @override
    def __repr__(self) -> str:
        return f"<LastGame id={self.match_id} hero={self.hero.name}>"

    @property
    def opendota(self) -> str:
        return f"opendota.com/matches/{self.match_id}"

    @property
    def stratz(self) -> str:
        return f"stratz.com/matches/{self.match_id}"

    @property
    def last_game_command_response(self) -> str:
        delta = datetime.datetime.now(datetime.UTC) - self.ended_dt
        game_type = self.wl_category.display_name(self.lobby_type, self.game_mode)
        outcome = self.outcome.name
        link = self.opendota if self.lobby_type == LobbyType.NewPlayerMode else self.stratz
        return (
            f"Last Game - {game_type}: {outcome} as {self.hero.name} {self.kda} "
            f"\N{MIDDLE DOT} ended {formats.timedelta_to_words(delta, fmt=formats.TimeDeltaFormat.Letter)} ago "
            f"\N{MIDDLE DOT} {link}"
        )


class Player:
    def __init__(self, bot: IrenesBot, account_id: int, hero_id: int, player_slot: int) -> None:
        self.bot: IrenesBot = bot
        self.account_id: int = account_id
        self.hero: Hero = Hero.try_value(hero_id)
        self.player_slot: int = player_slot

        self.lifetime_games: int | None = None
        self.medal: str | None = None
        self.is_data_ready: bool = False

        self.update.start()

    @override
    def __repr__(self) -> str:
        return f"<Player id={self.account_id} hero={self.hero.name}"

    @irenes_loop(seconds=5, count=12)
    async def update(self) -> None:
        partial_user = self.bot.dota.instantiate_partial_user(self.account_id)
        profile_card = await partial_user.dota2_profile_card()

        self.lifetime_games = profile_card.lifetime_games
        self.medal = rank_medal_display_name(profile_card)

        if self.medal:
            self.is_data_ready = True
            self.update.stop()

    @property
    def identifier(self) -> str:
        """Player Identifier so chat can recognize whom the data belongs to.

        * Either Hero name (i.e. "Windranger") if it's picked;
        * or Player Slot Colour (i.e. "Blue") otherwise.
        """
        return self.hero.name if self.hero else PLAYER_COLOURS[self.player_slot]

    @property
    def stratz(self) -> str:
        return f"stratz.com/players/{self.account_id}"

    def profile(self) -> str:
        return f"{self.identifier} {self.medal} \N{BULLET} {self.lifetime_games} total games \N{BULLET} {self.stratz}"


class Match(abc.ABC):
    def __init__(
        self,
        bot: IrenesBot,
        is_watch: bool,
        watching_server: str | None,
        *,
        unsupported_error: str = "",
    ) -> None:
        self.bot: IrenesBot = bot
        self.is_watch: bool = is_watch
        self.watching_server: str | None = watching_server
        self.server_steam_id: int | None = convert_id3_to_id64(watching_server) if watching_server else None

        self.match_id: int | None = None
        self.players: dict[int, Player] | None = None
        # ^ needs to be a dict so we can easily access a correct player to write missing data

        self.lobby_type: LobbyType | None = None
        self.game_mode: GameMode | None = None

        self.is_data_ready: bool = False
        self.is_hero_ready: bool = False

        self.unsupported_error: str = unsupported_error

    def game_medals(self) -> str:
        if not self.players:
            return "No players data yet."

        response_parts = [f"{player.identifier} {player.medal}" for player in self.players.values()]
        # \N{MIDDLE DOT}
        return " \N{BULLET} ".join(response_parts)  # [:5]) + " VS " + ", ".join(response_parts[5:])

    def ranked(self) -> str:
        if not self.lobby_type or not self.game_mode:
            return "No lobby data yet."

        yes_no = "Yes" if self.lobby_type == LobbyType.Ranked else "No"
        return f"{yes_no}, it's {self.lobby_type.display_name} ({self.game_mode.display_name})"

    def smurfs(self) -> str:
        if not self.players:
            return "No players data yet."

        response_parts = [f"{player.identifier} {player.lifetime_games}" for player in self.players.values()]
        return "Lifetime Games: " + " \N{BULLET} ".join(response_parts)

    async def profile(self, argument: str) -> str:
        if self.server_steam_id is None:
            return "This match doesn't support `real_time_stats`."
        if not self.players:
            return "No player data yet."

        try:
            match = await self.bot.steam_web_api.get_real_time_stats(self.server_steam_id)
        except:
            if self.lobby_type == LobbyType.NewPlayerMode:
                return "New Player Mode matches don't support `real_time_stats`."
            else:
                return "Failed to get `real_time_stats` for this match."

        if argument.isdigit():
            # then the user typed only a number and our life is easy because it is a player slot
            player_slot = int(argument) - 1
            if not 0 <= player_slot <= 9:
                return "Sorry, player_slot can only be of 1-10 values."
        else:
            # we have to use the fuzzy search
            the_choice = (None, 0)
            # first let's look in more official identifiers
            heroes = [player.hero for player in self.players.values()]
            for player_slot, hero in enumerate(heroes):
                identifiers = [PLAYER_COLOURS[player_slot]]
                if hero:
                    identifiers.extend([hero.name, hero.display_name])
                find = process.extractOne(argument, identifiers, score_cutoff=69)
                if find and find[1] > the_choice[1]:
                    the_choice = (player_slot, find[1])

            # second, let's see if hero aliases can beat official
            for player_slot, hero in enumerate(heroes):
                if hero:
                    find = process.extractOne(argument, HERO_ALIASES[hero.id], score_cutoff=69)
                    if find and find[1] > the_choice[1]:
                        the_choice = (player_slot, find[1])

            # BACK TO DICT
            player_slot = the_choice[0]
            if player_slot is None:
                return (
                    "Sorry, didn't understand your query. "
                    'Try something like !item "PA / 7 / PhantomAssassin / Blue" (but for your player).'
                )

        team = int(player_slot > 4)
        team_slot = player_slot - 5 * team

        api_player = match["teams"][team]["players"][team_slot]

        prefix = f"[2m delay] {Hero.try_value(api_player['heroid'])} lvl {api_player['level']}"
        net_worth = f"NW: {api_player['net_worth']}"
        kda = f"{api_player['kill_count']}/{api_player['death_count']}/{api_player['assists_count']}"
        cs = f"CS: {api_player['lh_count']}"

        items = ", ".join([await self.bot.cache_dota.item_by_id(item) for item in api_player["items"] if item != -1])
        link = f"stratz.com/players/{api_player['accountid']}"

        response_parts = (prefix, net_worth, kda, cs, items, link)
        return " \N{BULLET} ".join(response_parts)

    @abc.abstractmethod
    async def played_with(self, last_game: LastGame) -> str: ...


class PlayMatch(Match):
    """Represent a live match that a streamer is playing at the moment.

    Parameters
    ----------
    account_id:
        Streamer's steam_id32 id.
    watchable_game_id:
        Used to compare...
    watching_server:
        Used to compare...
    """

    def __init__(
        self,
        bot: IrenesBot,
        watchable_game_id: str,
        watching_server: str | None,
        account_id: int,
    ) -> None:
        super().__init__(bot, False, watching_server)
        self.watchable_game_id: str = watchable_game_id
        self.lobby_id: int = int(watchable_game_id)
        self.account_id: int = account_id

        self.hero: Hero | None = None
        self.average_mmr: int | None = None
        self.team: Team | None = None

        self.update_data.start()

    def reset(self) -> None:
        self.update_data.cancel()
        self.update_heroes.cancel()
        self.check_players.cancel()
        if self.players:
            for player in self.players.values():
                player.update.cancel()

    @irenes_loop(seconds=10, count=30)
    async def update_data(self) -> None:
        log.debug("Task `update_data` starts now: iteration=%s", self.update_data.current_loop)
        match = next(iter(await self.bot.dota.live_matches(lobby_ids=[self.lobby_id])), None)
        if not match:
            msg = "FindTopSourceTVGames didn't find such match."
            raise errors.PlaceholderRaiseError(msg)

        # match data
        self.match_id = match.id
        self.average_mmr = match.average_mmr
        self.lobby_type = match.lobby_type
        self.game_mode = match.game_mode

        # players
        self.players = {
            gc_player.id: Player(self.bot, gc_player.id, gc_player.hero.id, player_slot)
            for player_slot, gc_player in enumerate(match.players)
        }
        log.debug("Received Players in the match: %s", self.players)

        if self.update_data.current_loop == 0:
            if not self.check_heroes():
                self.update_heroes.start()

            self.check_players.start()

        if self.game_mode:
            self.update_data.stop()

    @irenes_loop(seconds=10, count=30)
    async def check_players(self) -> None:
        log.debug("Task `check_players` starts now: iteration=%s", self.check_players.current_loop)
        if self.players and all(player.is_data_ready for player in self.players.values()):
            # match data is ready
            self.is_data_ready = True
            self.bot.dispatch("match_data_ready")
            self.check_players.stop()

    def check_heroes(self) -> bool:
        if self.players and all(bool(player.hero) for player in self.players.values()):
            self.is_hero_ready = True
            self.hero = next(player.hero for player in self.players.values() if player.account_id == self.account_id)
            self.bot.dispatch("match_hero_ready")
            return True
        else:
            return False

    # count exists to prevent potential infinite loop ?
    # i'm not sure how it behaves when the object is null'ed.
    @irenes_loop(seconds=10, count=30)
    async def update_heroes(self) -> None:
        log.debug("Task `update_heroes` starts now: iteration=%s", self.update_heroes.current_loop)
        if self.update_heroes.current_loop == 0:
            return

        match = next(iter(await self.bot.dota.live_matches(lobby_ids=[self.lobby_id])), None)
        if not match:
            msg = "FindTopSourceTVGames didn't find such match."
            raise errors.PlaceholderRaiseError(msg)
        if not self.players:
            msg = f"Somehow update_heroes was started with {self.players=}."
            raise errors.PlaceholderRaiseError(msg)

        for gc_player in match.players:
            player = self.players[gc_player.id]
            if not player.hero:
                # need to fill the missing data in case previous times we got zeros
                player.hero = gc_player.hero

        if self.check_heroes():
            self.update_heroes.stop()

    @override
    def game_medals(self) -> str:
        mmr_notice = f"[{self.average_mmr} avg] " if self.average_mmr else ""
        return mmr_notice + super().game_medals()

    @override
    async def played_with(self, last_game: LastGame | None) -> str:
        if not self.players:
            return "No players data for this game yet."
        if not last_game:
            return "No last game data yet."

        overlap_player_ids = set(last_game.players.keys()).intersection(set(self.players.keys()))
        overlap_player_ids.remove(self.account_id)  # remove myself
        if not overlap_player_ids:
            return "No players from last game present in the match."
        else:
            return "Players from last game:" + " \N{BULLET} ".join(
                f"{self.players[player_id].identifier} was {last_game.players[player_id].name}"
                for player_id in overlap_player_ids
            )


class WatchMatch(Match):
    if TYPE_CHECKING:
        # if None - exception is raised in `streamer.update()`
        watching_server: str
        server_steam_id: int

    def __init__(self, bot: IrenesBot, watching_server: str) -> None:
        super().__init__(bot, True, watching_server)

    def reset(self) -> None:
        self.update_data.cancel()
        self.update_heroes.cancel()

    @irenes_loop(seconds=10, count=30)
    async def update_data(self) -> None:
        match = await self.bot.steam_web_api.get_real_time_stats(self.server_steam_id)

        # match data
        self.match_id = int(match["match"]["match_id"])
        self.lobby_type = LobbyType.try_value(match["match"]["lobby_type"])
        self.game_mode = GameMode.try_value(match["match"]["game_mode"])

        # players
        self.players = {
            api_player["accountid"]: Player(self.bot, api_player["accountid"], api_player["heroid"], player_slot)
            for player_slot, api_player in enumerate(
                itertools.chain(match["teams"][0]["players"], match["teams"][1]["players"])
            )
        }
        if self.update_data.current_loop == 0:
            if not self.check_heroes():
                self.update_heroes.start()

            self.check_players.start()

        if self.game_mode:
            self.update_data.stop()

    @irenes_loop(seconds=10, count=30)
    async def check_players(self) -> None:
        if self.players and all(player.is_data_ready for player in self.players.values()):
            # match data is ready
            self.is_data_ready = True
            self.bot.dispatch("match_data_ready")
            self.check_players.stop()

    def check_heroes(self) -> bool:
        if self.players and all(bool(player.hero) for player in self.players.values()):
            self.is_hero_ready = True
            self.bot.dispatch("match_hero_ready")
            return True
        else:
            return False

    @irenes_loop(seconds=10, count=30)
    async def update_heroes(self) -> None:
        if self.update_heroes.current_loop == 0:
            return

        if not self.players:
            msg = f"Somehow update_heroes was started with {self.players=}."
            raise errors.PlaceholderRaiseError(msg)

        match = await self.bot.steam_web_api.get_real_time_stats(self.server_steam_id)

        for api_player in itertools.chain(match["teams"][0]["players"], match["teams"][1]["players"]):
            player = self.players[api_player["accountid"]]
            if not player.hero:
                # need to fill the missing data in case previous times we got zeros
                self.players[api_player["accountid"]].hero = Hero.try_value(api_player["heroid"])

        if self.check_heroes():
            self.update_heroes.stop()

    @override
    async def played_with(self, _: LastGame | None) -> str:
        return "The command is not supported for spectated games."

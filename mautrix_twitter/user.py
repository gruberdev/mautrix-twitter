# mautrix-twitter - A Matrix-Twitter DM puppeting bridge
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from typing import Dict, Optional, List, Awaitable, Union, TYPE_CHECKING, cast
import asyncio
import logging

from mautwitdm import TwitterAPI
from mautwitdm.types import (MessageEntry, ReactionCreateEntry, ReactionDeleteEntry, Conversation,
                             User as TwitterUser)
from mautrix.bridge import BaseUser
from mautrix.types import UserID, RoomID
from mautrix.appservice import AppService

from .db import User as DBUser
from .config import Config
from . import puppet as pu, portal as po

if TYPE_CHECKING:
    from .__main__ import TwitterBridge


class User(DBUser, BaseUser):
    by_mxid: Dict[UserID, 'User'] = {}
    by_twid: Dict[int, 'User'] = {}
    config: Config
    az: AppService
    loop: asyncio.AbstractEventLoop

    client: Optional[TwitterAPI]

    is_admin: bool
    permission_level: str
    username: Optional[str]

    _notice_room_lock: asyncio.Lock

    def __init__(self, mxid: UserID, twid: Optional[int] = None, auth_token: Optional[str] = None,
                 csrf_token: Optional[str] = None, poll_cursor: Optional[str] = None,
                 notice_room: Optional[RoomID] = None) -> None:
        super().__init__(mxid=mxid, twid=twid, auth_token=auth_token, csrf_token=csrf_token,
                         poll_cursor=poll_cursor, notice_room=notice_room)
        self._notice_room_lock = asyncio.Lock()
        perms = self.config.get_permissions(mxid)
        self.is_whitelisted, self.is_admin, self.permission_level = perms
        self.log = self.log.getChild(self.mxid)
        self.client = None
        self.username = None

    @classmethod
    async def init_cls(cls, bridge: 'TwitterBridge') -> List[Awaitable[None]]:
        cls.config = bridge.config
        cls.az = bridge.az
        cls.loop = bridge.loop
        return [user.try_connect() for user in await cls.all_logged_in()]

    async def update(self) -> None:
        if self.client:
            self.auth_token, self.csrf_token = self.client.tokens
            self.poll_cursor = self.client.poll_cursor
        await super().update()

    # region Connection management

    async def is_logged_in(self) -> bool:
        try:
            return self.client and await self.client.get_user_identifier() is not None
        except Exception:
            return False

    async def try_connect(self) -> None:
        try:
            await self.connect()
        except Exception:
            self.log.exception("Error while connecting to Twitter")

    async def connect(self, auth_token: Optional[str] = None, csrf_token: Optional[str] = None
                      ) -> None:
        client = TwitterAPI(log=logging.getLogger("mau.twitter.api").getChild(self.mxid),
                            loop=self.loop, node_id=hash(self.mxid) % (2 ** 48))
        client.poll_cursor = self.poll_cursor
        client.set_tokens(auth_token or self.auth_token, csrf_token or self.csrf_token)

        # Initial ping to make sure auth works
        await client.get_user_identifier()

        self.client = client
        self.client.add_handler(Conversation, self.handle_conversation_update)
        self.client.add_handler(TwitterUser, self.handle_user_update)
        self.client.add_handler(MessageEntry, self.handle_message)
        self.client.add_handler(ReactionCreateEntry, self.handle_reaction)
        self.client.add_handler(ReactionDeleteEntry, self.handle_reaction)

        settings = await self.client.get_settings()
        self.username = settings["screen_name"]

        if not self.twid:
            user_info = await self.client.lookup_users(usernames=[self.username])
            self.twid = user_info[0].id
            self.by_twid[self.twid] = self

        await self.update()
        self.client.start()

    async def stop(self) -> None:
        if self.client:
            self.client.stop()
        await self.update()

    async def logout(self) -> None:
        if self.client:
            self.client.stop()
        puppet = await pu.Puppet.get_by_twid(self.twid, create=False)
        if puppet and puppet.is_real_user:
            await puppet.switch_mxid(None, None)
        try:
            del self.by_twid[self.twid]
        except KeyError:
            pass
        self.client = None
        self.twid = None
        self.poll_cursor = None
        self.auth_token = None
        self.csrf_token = None
        await self.update()

    # endregion
    # region Event handlers

    async def handle_conversation_update(self, evt: Conversation) -> None:
        portal = await po.Portal.get_by_twid(evt.conversation_id, self.twid, conv_type=evt.type)
        await portal.update_info(evt)

    async def handle_user_update(self, user: TwitterUser) -> None:
        puppet = await pu.Puppet.get_by_twid(user.id)
        await puppet.update_info(user)

    async def handle_message(self, evt: MessageEntry) -> None:
        portal = await po.Portal.get_by_twid(evt.conversation_id, self.twid,
                                             conv_type=evt.conversation.type)
        if not portal.mxid:
            await portal.create_matrix_room(self, evt.conversation)
        await portal.handle_twitter_message(self, evt.message_data, evt.request_id)

    async def handle_reaction(self, evt: Union[ReactionCreateEntry, ReactionDeleteEntry]) -> None:
        pass

    # endregion
    # region Database getters

    def _add_to_cache(self) -> None:
        self.by_mxid[self.mxid] = self
        if self.twid:
            self.by_twid[self.twid] = self

    @classmethod
    async def get_by_mxid(cls, mxid: UserID, create: bool = True) -> Optional['User']:
        try:
            return cls.by_mxid[mxid]
        except KeyError:
            pass

        user = cast(cls, await super().get_by_mxid(mxid))
        if user is not None:
            user._add_to_cache()
            return user

        if create:
            user = cls(mxid)
            await user.insert()
            user._add_to_cache()
            return user

        return None

    @classmethod
    async def get_by_twid(cls, twid: int) -> Optional['User']:
        try:
            return cls.by_twid[twid]
        except KeyError:
            pass

        user = cast(cls, await super().get_by_twid(twid))
        if user is not None:
            user._add_to_cache()
            return user

        return None

    @classmethod
    async def all_logged_in(cls) -> List['User']:
        users = await super().all_logged_in()
        user: cls
        for index, user in enumerate(users):
            try:
                users[index] = cls.by_mxid[user.mxid]
            except KeyError:
                user._add_to_cache()
        return users

    # endregion
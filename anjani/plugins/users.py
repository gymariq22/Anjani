"""User data management"""
# Copyright (C) 2020 - 2021  UserbotIndo Team, <https://github.com/userbotindo.git>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import asyncio
import re
from hashlib import md5
from typing import ClassVar, List, Optional, Union

from aiopath import AsyncPath
from pyrogram.errors import PeerIdInvalid
from pyrogram.types import Chat, ChatPreview, Message, User

from anjani import command, plugin, util


class Users(plugin.Plugin):
    name: ClassVar[str] = "Users"

    chats_db: util.db.AsyncCollection
    users_db: util.db.AsyncCollection
    lock: asyncio.locks.Lock
    predict_loaded: bool

    async def on_load(self) -> None:
        self.chats_db = self.bot.db.get_collection("CHATS")
        self.users_db = self.bot.db.get_collection("USERS")
        self.predict_loaded = False

        async def c_pred():
            await asyncio.sleep(2)  # wait for model download
            if "SpamPredict" in self.bot.plugins:
                self.predict_loaded = True

        self.bot.loop.create_task(c_pred())

    def hash_id(self, id: int) -> str:
        return md5((str(id) + self.bot.user.username).encode()).hexdigest()  # skipcq: PTC-W1003

    async def build_channel_task(self, channel: Chat) -> Optional[asyncio.Task]:
        if channel.type == "channel":
            data = await self.chats_db.find_one({"chat_id": channel.id})
            content = {"chat_name": channel.title, "type": channel.type}
            if not data or "hash" not in data:
                content["hash"] = self.hash_id(channel.id)
            return self.bot.loop.create_task(
                self.chats_db.update_one({"chat_id": channel.id}, {"$set": content}, upsert=True)
            )
        return None

    async def on_chat_migrate(self, message: Message) -> None:
        new_chat = message.chat.id
        old_chat = message.migrate_from_chat_id

        await asyncio.gather(
            self.users_db.update_many({"chats": old_chat}, {"$push": {"chats": new_chat}}),
            self.users_db.update_many({"chats": old_chat}, {"$pull": {"chats": old_chat}}),
            self.chats_db.update_one({"chat_id": old_chat}, {"$set": {"chat_id": new_chat}}),
        )

    async def on_chat_action(self, message: Message) -> None:
        """Delete user data from chats"""
        if message.new_chat_members:
            return

        chat = message.chat
        user = message.left_chat_member
        if user.id == self.bot.uid:
            await asyncio.gather(
                self.chats_db.delete_one({"chat_id": chat.id}),
                self.users_db.update_many({"chats": chat.id}, {"$pull": {"chats": chat.id}}),
            )
        else:
            await asyncio.gather(
                self.users_db.update_one({"_id": user.id}, {"$pull": {"chats": chat.id}}),
                self.chats_db.update_one({"chat_id": chat.id}, {"$pull": {"member": user.id}}),
            )

    async def on_message(self, message: Message) -> None:
        """Incoming message handler."""
        chat = message.chat
        user = message.from_user

        if not user or not chat:  # sanity check for service
            return

        tasks = []
        set_content = {"username": user.username}
        user_data = await self.users_db.find_one({"_id": user.id})

        if chat.type == "private":
            if self.predict_loaded:
                if ch := message.forward_from_chat:
                    tasks.append(await self.build_channel_task(ch))
                if not user_data or "hash" not in user_data:
                    set_content["hash"] = self.hash_id(user.id)
            await asyncio.gather(
                self.users_db.update_one({"_id": user.id}, {"$set": set_content}), *tasks
            )
            return

        chat_data = await self.chats_db.find_one({"chat_id": chat.id})
        chat_update = {
            "$set": {"chat_name": chat.title, "type": chat.type},
            "$addToSet": {"member": user.id},
        }
        if self.predict_loaded:
            if not user_data or "hash" not in user_data:
                set_content["hash"] = self.hash_id(user.id)
            if not chat_data or "hash" not in chat_data:
                chat_update["$set"]["hash"] = self.hash_id(chat.id)
            update = {
                "$set": set_content,
                "$setOnInsert": {"reputation": 0},
                "$addToSet": {"chats": chat.id},
            }
            if ch := message.forward_from_chat:
                tasks.append(await self.build_channel_task(ch))
        else:
            update = {"$set": {"username": user.username}, "$addToSet": {"chats": chat.id}}

        await asyncio.gather(
            self.users_db.update_one({"_id": user.id}, update, upsert=True),
            self.chats_db.update_one({"chat_id": chat.id}, chat_update, upsert=True),
            *tasks,
        )

    async def _user_info(self, ctx: command.Context, user: User):
        """User Info"""
        text = f"**{'Bot' if user.is_bot else 'User'} Info**\n\n"
        text += f"**ID:** `{user.id}`\n"
        text += f"**DC ID: **`{user.dc_id if user.dc_id else 'N/A'}`\n"
        text += f"**First Name: **{user.first_name}\n"
        if user.last_name:
            text += f"**Last Name: **{user.last_name}\n"
        if user.username:
            text += f"**Username: **@{user.username}\n"
        text += f"**Permanent user link: **{user.mention}\n"
        text += (
            "**Number of profile pics: **"
            f"`{await self.bot.client.get_profile_photos_count(user.id)}`\n"
        )
        if user.status:
            text += f"**Last seen: ** `{user.status}`\n"
        if user.id in self.bot.staff:
            if user.id == self.bot.owner:
                text += "\nThis person is my **owner**!\nI would never do anything against him.\n"
            else:
                text += "\nThis person is one of my **Devs**!\nNearly as powerfull as my owner.\n"
        elif user.is_self:
            text += "\nI've seen them in every chats... wait it's me!!\nWow you're stalking me? 😂"

        user_db = await self.users_db.find_one({"_id": user.id})
        if user_db:
            if self.predict_loaded:
                text += f"\n**Identifier:** `{user_db.get('hash', 'unknown')}`"
                text += f"\n**Reputation: **`{user_db.get('reputation', 0)}`"
            text += f"\nI've seen them on {len(user_db['chats'])} chats."

        if user.photo:
            async with ctx.action("upload_photo"):
                file = AsyncPath(await self.bot.client.download_media(user.photo.big_file_id))
                await self.bot.client.send_photo(
                    ctx.chat.id, str(file), text, reply_to_message_id=ctx.message.message_id
                )
                await file.unlink()
            return None

        return text

    async def _chat_info(self, ctx: command.Context, chat: Union[Chat, ChatPreview]):
        """Chat Info"""
        text = "**Chat info**\n\n"
        if isinstance(chat, ChatPreview):
            text += f"**Chat Type:** `{chat.type}`\n"
            text += f"**Title:** `{chat.title}`\n"
            text += f"**Member Count:** `{chat.members_count}`\n"
        else:
            text += f"**ID:** `{chat.id}`\n"
            if chat.dc_id:
                text += f"**DC ID:** `{chat.dc_id}`\n"
            text += f"**Chat Type:** `{chat.type}`\n"
            text += f"**Title:** `{chat.title}`\n"
            if chat.username:
                text += f"**Chat Username:** @{chat.username}\n"
            text += f"**Member Count:** `{chat.members_count}`\n"
            if chat.linked_chat:
                text += f"**Linked Chat:** `{chat.linked_chat.title}`\n"

            if self.predict_loaded:
                chat_data = await self.chats_db.find_one({"chat_id": chat.id})
                if chat_data:
                    text += f"**Identifier:** `{chat_data.get('hash', 'unknown')}`\n"

        if chat.photo:
            async with ctx.action("upload_photo"):
                file = AsyncPath(await self.bot.client.download_media(chat.photo.big_file_id))
                await self.bot.client.send_photo(
                    ctx.chat.id, str(file), text, reply_to_message_id=ctx.message.message_id
                )
                await file.unlink()
            return None
        return text

    async def cmd_info(self, ctx: command.Context, args: Optional[str]):
        """Fetch a telegram peer data"""
        if not args:
            if ctx.msg.reply_to_message:
                return await self._user_info(ctx, ctx.msg.reply_to_message.from_user)
            if not ctx.msg.reply_to_message:
                return await self._user_info(ctx, ctx.author)
            return

        id_match = None
        if self.predict_loaded:
            id_match = re.search(r"([a-fA-F\d]{32})", args)
        if id_match:
            user = await self.users_db.find_one({"hash": id_match.group(0)})
            if user:
                user = await ctx.bot.client.get_users(user["_id"])
                if isinstance(user, List):
                    user = user[0]
                return await self._user_info(ctx, user)
            chat = await self.chats_db.find_one({"hash": id_match.group(0)})
            if chat:
                chat = await ctx.bot.client.get_chat(chat["chat_id"])
                return await self._chat_info(ctx, chat)
            return await self.text(ctx.chat.id, "err-invalid-pid")

        try:
            user = await ctx.bot.client.get_users(args)
            if isinstance(user, List):
                user = user[0]
            return await self._user_info(ctx, user)
        except (IndexError, PeerIdInvalid):  # chat peer
            try:
                chat = await ctx.bot.client.get_chat(args)
                return await self._chat_info(ctx, chat)
            except PeerIdInvalid:
                return await self.text(ctx.chat.id, "err-peer-invalid")

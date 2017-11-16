import asyncio
import io
import sys
import textwrap
import traceback
import zlib
from collections import defaultdict
from typing import *

import enet

from acelib import packets, math3d, world
from acelib.bytes import ByteReader, ByteWriter
from acelib.constants import *
from aceserver import base, protocol, types, weapons, util
from aceserver.loaders import *


_loader_handlers: Dict[int, Callable[['ServerConnection', packets.Loader], None]] = {}
def on_loader_receive(*loaders):
    def decorator(func):
        _loader_handlers.update({loader.id: func for loader in loaders})
        return func
    return decorator


class ServerConnection(base.BaseConnection):
    def __init__(self, protocol: 'protocol.ServerProtocol', peer: enet.Peer):
        self.protocol = protocol
        self.peer = peer

        self.id: int = None
        self.name: str = "Deuce"
        self.hp = 100
        self.team: types.Team = None

        self.weapon = weapons.Weapon(self)
        self.block = weapons.Block(self)
        self.spade = weapons.Tool(self)
        self.grenade = weapons.Grenade(self)
        self.tool_type = TOOL.WEAPON

        self.kills: int = 0

        self.wo: world.Player = world.Player(self.protocol.map)
        self.store = {}

        self._listeners: Dict[int, List[asyncio.Future]] = defaultdict(list)

    async def on_connect(self, data: int):
        if data != PROTOCOL_VERSION:
            return await self.disconnect(DISCONNECT.WRONG_VERSION)

        self.protocol.loop.create_task(self.connection_ack())

    async def on_disconnect(self):
        if self.id is not None:
            await self.on_player_leave(self)

    async def on_receive(self, packet: enet.Packet):
        try:
            reader: ByteReader = ByteReader(packet.data, packet.dataLength)
            packet_id: int = reader.read_uint8()
            loader: packets.Loader = packets.CLIENT_LOADERS[packet_id](reader)
        except:
            print(f"Malformed packet from player #{self.id}, disconnecting.", file=sys.stderr)
            traceback.print_exc()
            return await self.disconnect()
        await self.received_loader(loader)

    def send_loader(self, loader: packets.Loader, flags=enet.PACKET_FLAG_RELIABLE):
        writer: ByteWriter = loader.generate()
        packet: enet.Packet = enet.Packet(bytes(writer), flags)
        # does this even DO anything?? enet just queues the packet for the next host_service call...
        # perhaps sending doesnt need to be coros, but receiving should.
        # please send help i have no clue what im doing
        return self.protocol.loop.run_in_executor(None, self.peer.send, 0, packet)

    async def disconnect(self, reason: DISCONNECT=DISCONNECT.UNDEFINED):
        self.peer.disconnect(reason)

    async def received_loader(self, loader: packets.Loader):
        if self.id is None:
            print(loader)
            return await self.disconnect()

        listeners = self._listeners.pop(loader.id, ())
        for fut in listeners:
            if fut.done():
                continue
            fut.set_result(loader)

        handler = _loader_handlers.get(loader.id)
        if not handler:
            if not listeners:
                print(f"Warning: unhandled packet {loader.id}:{loader} from player #{self.id}")
        else:
            # print(f"Received {loader.id}:{loader} from player #{self.player_id}")
            await handler(self, loader)

    def wait_for(self, loader: Type[packets.Loader], timeout=None) -> Coroutine[Any, Any, packets.Loader]:
        fut = self.protocol.loop.create_future()
        self._listeners[loader.id].append(fut)
        return asyncio.wait_for(fut, timeout, loop=self.protocol.loop)

    async def connection_ack(self):
        self.id = self.protocol.player_ids.pop()
        await self.send_packs()
        await self.send_map()
        await self.send_state()
        await self.send_players()
        await self.on_player_connect(self)

    async def send_packs(self):
        for data, length, crc32 in self.protocol.packs:
            pack_start.checksum = crc32
            pack_start.size = length
            await self.send_loader(pack_start)

            response: packets.PackResponse = await self.wait_for(packets.PackResponse)
            if not response.value:  # client does NOT have pack cached
                with io.BytesIO(data) as f:
                    while True:
                        data = f.read(1024)
                        if not data:
                            break
                        pack_chunk.data = data
                        await self.send_loader(pack_chunk)

    async def send_map(self):
        map: bytes = zlib.compress(self.protocol.map.get_bytes(), 9)
        map_start.size = len(map)
        await self.send_loader(map_start)

        with io.BytesIO(map) as f:
            while True:
                data = f.read(1024)
                if not data:
                    break
                map_chunk.data = data
                await self.send_loader(map_chunk)

    async def send_state(self):
        data = self.protocol.get_state()
        data.player_id = self.id
        await self.send_loader(data)

    async def send_players(self):
        for conn in self.protocol.players.values():
            await self.send_loader(conn.to_existing_player())

    async def spawn(self, x: float=None, y: float=None, z: float=None):
        pos = self.protocol.mode.get_spawn_point(self) if x is None or y is None or z is None else (x, y, z)

        hook = await self.try_player_spawn(self, x, y, z)
        if hook is False:
            return
        pos = pos if hook is None else hook

        create_player.position.xyz = pos
        create_player.weapon = self.weapon.type
        create_player.player_id = self.id
        create_player.name = self.name
        create_player.team = self.team.id
        await self.protocol.broadcast_loader(create_player)

        self.wo.set_dead(False)
        self.wo.set_position(*pos, reset=True)
        await self.restock()
        await self.on_player_spawn(self, x, y, z)

    async def set_hp(self, hp: int, reason: DAMAGE=None, source: tuple=(0, 0, 0)):
        if reason is None:
            if hp >= self.hp:
                reason = DAMAGE.HEAL
            else:
                reason = DAMAGE.SELF
        self.hp = max(0, min(int(hp), 100))
        set_hp.hp = self.hp
        set_hp.type = reason
        set_hp.source.xyz = source
        await self.send_loader(set_hp)

    async def kill(self, kill_type: KILL=KILL.FALL, killer=None):
        if self.dead or self.store.get("respawn_task") is not None: return

        self.wo.set_dead(True)

        respawn_time = self.protocol.get_respawn_time()
        kill_action.player_id = self.id
        kill_action.killer_id = (killer or self).id
        kill_action.kill_type = kill_type
        kill_action.respawn_time = respawn_time + 1
        await self.protocol.broadcast_loader(kill_action)
        self.store["respawn_task"] = self.protocol.loop.create_task(self.respawn(respawn_time))

    async def respawn(self, respawn_time=0):
        await asyncio.sleep(respawn_time)
        await self.spawn()
        self.store.pop("respawn_task")

    async def hurt(self, damage: int, cause: KILL=KILL.FALL, damager=None, source=(0, 0, 0)):
        reason = DAMAGE.OTHER
        if not source:
            if damager is not None:
                source = damager.position.xyz
            else:
                source = self.position.xyz
                reason = DAMAGE.SELF

        hook = await self.try_player_hurt(self, damage, damager, cause)
        if hook is False:
            return
        damage = damage if hook is None else hook
        await self.set_hp(self.hp - damage, reason, source)
        if self.hp <= 0:
            await self.kill(cause, damager)
        else:
            await self.on_player_hurt(self, damage, damager, reason)

    async def set_tool(self, tool: TOOL):
        self.tool.set_primary(False)
        self.tool.set_secondary(False)
        self.tool_type = tool

        set_tool.player_id = self.id
        set_tool.value = tool
        await self.protocol.broadcast_loader(set_tool)

    async def destroy_block(self, x: int, y: int, z: int, destroy_type: ACTION=ACTION.DESTROY):
        hook = await self.try_destroy_block(self, x, y, z, destroy_type)
        if hook is False:
            return False
        if hook is not None:
            x, y, z = hook

        to_destroy = [(x, y, z)]
        if destroy_type == ACTION.SPADE and self.tool_type == TOOL.SPADE:
            to_destroy.extend(((x, y, z - 1), (x, y, z + 1)))
        elif destroy_type == ACTION.GRENADE:
            for ax in range(x - 1, x + 2):
                for ay in range(y - 1, y + 2):
                    for az in range(z - 1, z + 2):
                        to_destroy.append((ax, ay, az))
        elif destroy_type == ACTION.DESTROY:
            self.block.destroy()

        for ax, ay, az in to_destroy:
            if self.protocol.map.can_build(ax, ay, az):
                self.protocol.map.set_point(ax, ay, az, False)

        block_action.player_id = self.id
        block_action.xyz = (x, y, z)
        block_action.value = destroy_type
        await self.protocol.broadcast_loader(block_action)
        self.protocol.loop.create_task(self.on_destroy_block(self, x, y, z, destroy_type))
        return True

    async def build_block(self, x: int, y: int, z: int, color: Tuple[int, int, int]=None) -> bool:
        if not self.protocol.map.can_build(x, y, z):
            return False

        if not self.block.build():
            return False

        if color is not None:
            await self.block.set_color(*color)

        hook = await self.try_build_block(self, x, y, z)
        if hook is False:
            return False
        if hook is not None:
            x, y, z = hook
        r, g, b = self.block.color.rgb
        if self.protocol.map.set_point(x, y, z, True, (0x7F << 24) | r << 16 | g << 8 | b << 0):
            block_action.player_id = self.id
            block_action.xyz = (x, y, z)
            block_action.value = ACTION.BUILD
            await self.protocol.broadcast_loader(block_action)
            self.protocol.loop.create_task(self.on_build_block(self, x, y, z))
            return True
        return False

    async def set_position(self, x=None, y=None, z=None, reset=True):
        if x is None or y is None:
            x, y, z = self.position.xyz
        else:
            if z is None:
                z = self.protocol.map.get_z(x, y) - 2
        print(f"Setting pos to {x}, {y}, {z}")
        self.wo.set_position(x, y, z, reset)
        position_data.data.xyz = x, y, z
        await self.send_loader(position_data)

    async def restock(self):
        await self.set_hp(100)
        [tool.restock() for tool in self.tools]
        await self.send_loader(restock)

    async def play_sound(self, sound: types.Sound):
        pkt = sound.to_play_sound()
        await self.send_loader(pkt)

    # Chat Message Related
    @util.static_vars(wrapper=textwrap.TextWrapper(width=MAX_CHAT_SIZE))
    async def send_message(self, message: str, chat_type=CHAT.SYSTEM, player_id=0xFF):
        chat_message.chat_type = chat_type
        chat_message.player_id = player_id
        lines: List[str] = self.send_message.wrapper.wrap(message)
        for line in lines:
            chat_message.value = line
            await self.send_loader(chat_message)

    def send_chat_message(self, message: str, sender: 'ServerConnection', team: bool=False):
        chat_type = CHAT.TEAM if team else CHAT.ALL
        return self.send_message(message, player_id=sender.id, chat_type=chat_type)

    def send_server_message(self, message: str):
        return self.send_message("[*] " + message, chat_type=CHAT.SYSTEM)

    def send_hud_message(self, message: str):
        return self.send_message(message, chat_type=CHAT.BIG)

    @on_loader_receive(packets.PositionData)
    async def recv_position_data(self, loader: packets.PositionData):
        if self.dead: return

        x, y, z = loader.data.xyz
        if util.bad_float(x, y, z):
            return await self.disconnect()

        pos: math3d.Vector3 = math3d.Vector3(x, y, z)
        if pos.sq_distance(self.wo.position) >= 3 ** 2:
            await self.set_position(reset=False)
        else:
            self.wo.set_position(x, y, z)

    @on_loader_receive(packets.OrientationData)
    async def recv_orientation_data(self, loader: packets.OrientationData):
        if self.dead: return

        x, y, z = loader.data.xyz
        if util.bad_float(x, y, z):
            return await self.disconnect()

        self.wo.set_orientation(x, y, z)

    @on_loader_receive(packets.InputData)
    async def recv_input_data(self, loader: packets.InputData):
        if self.dead: return

        self.wo.set_walk(loader.up, loader.down, loader.left, loader.right)
        self.wo.set_animation(loader.jump, loader.crouch, loader.sneak, loader.sprint)
        loader.player_id = self.id
        await self.protocol.broadcast_loader(loader, predicate=lambda conn: conn != self)

    @on_loader_receive(packets.ExistingPlayer)
    async def recv_existing_player(self, loader: packets.ExistingPlayer):
        if loader.weapon not in weapons.WEAPONS or loader.team not in self.protocol.teams:
            return await self.disconnect()

        self.name = self.validate_name(loader.name)
        self.weapon = weapons.WEAPONS[loader.weapon](self)
        self.team = self.protocol.teams[loader.team]
        # await self.protocol.player_joined(self)
        await self.on_player_join(self)
        await self.spawn()

    def validate_name(self, name: str):
        name = name.strip()
        if name.isspace() or name == "Deuce":
            name = f"Deuce{self.id}"

        new_name = name
        existing_names = [ply.name.lower() for ply in self.protocol.players.values()]
        x = 0
        while new_name in existing_names:
            new_name = f"{name}{x}"
            x += 1
        return new_name

    @on_loader_receive(packets.ChatMessage)
    async def recv_chat_message(self, loader: packets.ChatMessage):
        if loader.chat_type not in (CHAT.TEAM, CHAT.ALL):
            return
        hook = await self.try_chat_message(self, loader.value, loader.chat_type)
        if hook is False:
            return
        message = hook or loader.value
        if loader.chat_type == CHAT.TEAM:
            await self.team.broadcast_chat_message(message, sender=self)
        else:
            await self.protocol.broadcast_chat_message(message, sender=self)
        await self.on_chat_message(self, loader.value, loader.chat_type)

        # chat_message.player_id = self.id
        # chat_message.chat_type = ChatType.ALL if loader.chat_type == ChatType.ALL else ChatType.TEAM
        # chat_message.value = loader.value
        # predicate = lambda conn: conn.team == self.team if loader.chat_type == ChatType.TEAM else None
        # await self.protocol.broadcast_loader(chat_message, predicate=predicate)

    @on_loader_receive(packets.BlockAction)
    async def recv_block_action(self, loader: packets.BlockAction):
        if self.dead: return

        if loader.value == ACTION.GRENADE:
            return  # client shouldnt send this

        if loader.value == ACTION.BUILD:
            await self.build_block(loader.x, loader.y, loader.z)
        else:
            await self.destroy_block(loader.x, loader.y, loader.z, loader.value)

    @on_loader_receive(packets.WeaponInput)
    async def recv_weapon_input(self, loader: packets.WeaponInput):
        if self.dead: return

        loader.primary = self.tool.set_primary(loader.primary)
        loader.secondary = self.tool.set_secondary(loader.secondary)
        loader.player_id = self.id
        self.wo.set_fire(loader.primary, loader.secondary)
        await self.protocol.broadcast_loader(loader, predicate=lambda conn: conn != self)

    @on_loader_receive(packets.WeaponReload)
    async def recv_weapon_reload(self, loader: packets.WeaponReload):
        if self.dead or self.tool_type != TOOL.WEAPON: return
        await self.tool.reload()

    @on_loader_receive(packets.SetTool)
    async def recv_set_tool(self, loader: packets.SetTool):
        if self.dead: return

        self.wo.set_weapon(loader.value == TOOL.WEAPON)
        await self.set_tool(loader.value)

    @on_loader_receive(packets.SetColor)
    async def recv_set_color(self, loader: packets.SetColor):
        if self.dead or self.tool_type != TOOL.BLOCK: return

        self.color = loader.color.rgb
        loader.player_id = self.id
        await self.protocol.broadcast_loader(loader, predicate=lambda conn: conn != self)

    @on_loader_receive(packets.UseOrientedItem)
    async def recv_oriented_item(self, loader: packets.UseOrientedItem):
        if self.dead:
            return

        if util.bad_float(*loader.position.xyz, *loader.velocity.xyz, loader.value):
            return await self.disconnect()

        obj_type = None
        if loader.tool == TOOL.GRENADE:
            if self.tool_type != TOOL.GRENADE:
                return
            if self.grenade.on_primary():
                obj_type = types.Grenade
        elif loader.tool == TOOL.RPG:
            if self.tool_type != TOOL.WEAPON or self.tool.type != WEAPON.RPG:
                return
            if self.weapon.on_primary():
                obj_type = types.Rocket
                # note: loader.velocity is actually orientation for RPG rockets.

        if obj_type is not None:
            obj: types.Explosive = \
                self.protocol.create_object(obj_type, self, self.position.xyz, loader.velocity.xyz, loader.value)
            await obj.broadcast_item(lambda conn: conn != self)

    @on_loader_receive(packets.HitPacket)
    async def recv_oriented_item(self, loader: packets.HitPacket):
        if self.dead:
            return
        if self.tool_type != TOOL.WEAPON or not self.tool.primary:
            return

        # TODO our own raycasting and hack detection etc.
        damage = self.weapon.get_damage(loader.value)
        if damage is None:
            return
        other = self.protocol.players.get(loader.player_id)
        if other is None:
            return

        vec = (other.eye - self.eye)
        vec.normalize()
        if not self.orientation.equals(vec, tolerance=1):
            print(f"self.pos={self.position} other.pos={other.position}")
            print(f"self.orien={self.orientation} valid_orien={vec}")
            return

        # await other.hurt(damage=damage, cause=KILL.HEADSHOT if loader.value == HIT.HEAD else KILL.WEAPON, damager=self)

    async def update(self, dt):
        if self.dead: return

        fall_dmg: int = self.wo.update(dt, self.protocol.time)
        if fall_dmg > 0:
            await self.hurt(fall_dmg)
        await self.tool.update(dt)

    def to_existing_player(self) -> packets.ExistingPlayer:
        existing_player.name = self.name
        existing_player.player_id = self.id
        existing_player.tool = self.tool_type or TOOL.WEAPON
        existing_player.weapon = self.weapon.type
        existing_player.kills = self.kills
        existing_player.team = self.team.id
        existing_player.color.rgb = self.block.color.rgb
        return existing_player

    @property
    def tool(self) -> weapons.Tool:
        return self.tools[self.tool_type]

    @property
    def tools(self) -> List[weapons.Tool]:
        return [self.spade, self.block, self.weapon, self.grenade]

    @property
    def position(self) -> math3d.Vector3:
        return self.wo.position

    @property
    def eye(self) -> math3d.Vector3:
        return self.wo.eye

    @property
    def orientation(self) -> math3d.Vector3:
        return self.wo.orientation

    @property
    def dead(self) -> bool:
        return self.wo.dead


    # TODO more hooks
    on_player_connect = util.AsyncEvent()


    # Called after the player joins the game
    # (self) -> None
    on_player_join = util.AsyncEvent()
    # Calls after the player disconnects from the game.
    # (self) -> None
    on_player_leave = util.AsyncEvent()

    # Called before/after spawning the player
    # (self, x, y, z) -> None | `x, y, z` to override | False to cancel
    try_player_spawn = util.AsyncEvent(overridable=True)
    # (self, x, y, z) -> None
    on_player_spawn = util.AsyncEvent()

    # Called before/after the player is hurt
    # (self, damage, damager, position) -> None | `damage` to override | False to cancel
    try_player_hurt = util.AsyncEvent(overridable=True)
    # (self, damage, damager, position) -> None
    on_player_hurt = util.AsyncEvent()

    # Called before/after the player builds
    # (self, x, y, z) -> None | `x, y, z` to override | False to cancel
    try_build_block = util.AsyncEvent(overridable=True)
    # (self, x, y, z) -> None
    on_build_block = util.AsyncEvent()

    # Called before/after the player destroys
    # (self, x, y, z, destroy_type) -> None | `x, y, z` to override | False to cancel
    try_destroy_block = util.AsyncEvent(overridable=True)
    # (self, x, y, z, destroy_type) -> None
    on_destroy_block = util.AsyncEvent()

    # Called before/after the player sends a chat message
    # (self, chat_message, chat_type) -> None | `chat_message` to override | False to cancel
    try_chat_message = util.AsyncEvent(overridable=True)
    # (self, chat_message, chat_type) -> None
    on_chat_message = util.AsyncEvent()

    def __str__(self):
        return self.name

    def __repr__(self):
        return f"<{self.__class__.__name__}(id={self.id}, name={self.name}, pos={self.position}, tool={self.tool})>"

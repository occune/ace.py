"""
The (eventually) default set of commands

Creator: 10se1ucgo
"""
import asyncio
import inspect

from acelib.world import cast_ray
from aceserver import types
from aceserver.protocol import ServerProtocol
from aceserver.connection import ServerConnection
from acescripts import Script, commands


class EssentialsScript(Script):
    def __init__(self, protocol: ServerProtocol, cfg: dict):
        super().__init__(protocol, cfg)
        # TODO is this the best way to add commands?
        # Perhaps the commands script should check each script for commands by itself.
        self.protocol.scripts.get("commands").add_commands(self)

    def deinit(self):
        self.protocol.scripts.get("commands").remove_commands(self)

    @commands.command()
    async def setpos(self, connection: ServerConnection, x: float, y: float, z: float=None):
        await connection.set_position(x, y, z)

    @commands.command()
    async def sethp(self, connection: ServerConnection, hp: int):
        await connection.set_hp(hp)

    @commands.command()
    async def hurt(self, connection: ServerConnection, hp: int):
        await connection.hurt(hp)

    @commands.command()
    async def tp(self, connection: ServerConnection, other: ServerConnection):
        await connection.set_position(*other.position.xyz)

    @commands.command()
    async def goto(self, connection: ServerConnection, grid_pos: str):
        letter = grid_pos[0].lower()
        number = int(grid_pos[1])

        if not ord('a') <= ord(letter) <= ord('h'):
            return
        if not 1 <= number <= 8:
            return

        x = 32 + (64 * (ord(letter) - ord('a')))
        y = 32 + (64 * (number - 1))
        print(x, y)
        await connection.set_position(x, y)

    @commands.command()
    async def spawn(self, connection: ServerConnection, type: types.Entity, num: int=1, x: float=None, y: float=None, z: float=None):
        for j in range(num):
            if x is None or y is None:
                pos = connection.protocol.mode.get_random_pos(connection.team)
            else:
                if z is None:
                    z = self.protocol.map.get_z(x, y)
                pos = (x, y, z)
            await self.protocol.create_entity(ent_type=type, position=pos, team=connection.team)

    @commands.command()
    async def test_raycast(self, connection: ServerConnection):
        while True:
            pos = cast_ray(connection.protocol.map, connection.position, connection.orientation, length=256)
            if not pos:
                continue
            await connection.destroy_block(*pos)
            await asyncio.sleep(1 / 10)

    @commands.command()
    async def grenade(self, connection: ServerConnection, x: float, y: float, z: float, a: float=0, b: float=0, c: float=0):
        obj = self.protocol.create_object(types.Grenade, connection, (x, y, z), (a, b, c))
        eta, pos = obj.next_collision(dt=1/60, max=20)
        if eta is not False:
            obj.fuse = eta
        await obj.broadcast_item()

    @commands.command()
    async def fog(self, connection: ServerConnection, r: int, g: int, b: int):
        await self.protocol.set_fog_color(r, g, b)

    @commands.command()
    async def a(self, connection: ServerConnection, source: str):
        ret = eval(source)
        if inspect.isawaitable(ret):
            ret = await ret
        print(ret)

    @commands.command()
    async def b(self, connection: ServerConnection, source: str):
        exec(source)


def init(protocol: ServerProtocol, cfg: dict):
    return EssentialsScript(protocol, cfg)

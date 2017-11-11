import asyncio

from acelib.constants import *
from acelib import packets
from aceserver import connection, loaders


class Tool:
    name: str = "None"
    type = None

    max_primary: int = 0
    max_secondary: int = 0

    primary_rate: float = 0
    secondary_rate: float = 0

    def __init__(self, connection: 'connection.ServerConnection'):
        self.connection = connection
        self.next_primary = connection.protocol.time
        self.next_secondary = connection.protocol.time

        self.primary = False
        self.secondary = False

        self.primary_ammo = self.max_primary
        self.secondary_ammo = self.max_secondary

    async def update(self, dt: float):
        if self.primary_rate:
            if self.primary_ammo > 0 and self.primary and self.connection.protocol.time >= self.next_primary:
                self.next_primary = self.connection.protocol.time + self.primary_rate
                self.on_primary()

        if self.secondary_rate:
            if self.secondary_ammo > 0 and self.secondary and self.connection.protocol.time >= self.next_secondary:
                self.next_secondary = self.connection.protocol.time + self.primary_rate
                self.on_secondary()

    def set_primary(self, primary: bool):
        self.primary = primary
        return primary

    def set_secondary(self, secondary: bool):
        self.secondary = secondary
        return secondary

    def on_primary(self, *args, **kwargs):
        return True

    def on_secondary(self, *args, **kwargs):
        return True

    async def reload(self, *args, **kwargs):
        return True

    def restock(self):
        self.primary_ammo = self.max_primary  # bug? client seems to think it should
        self.secondary_ammo = self.max_secondary

    def reset(self):
        pass


class Block(Tool):
    max_primary = 50

    def __init__(self, connection: 'connection.ServerConnection'):
        super().__init__(connection)
        self.color = packets.Color()
        self.color.rgb = (112, 112, 112)

    def build(self):
        if self.primary_ammo > 0:
            self.primary_ammo -= 1
            return True
        return False

    def destroy(self):
        self.primary_ammo = max(0, min(self.primary_ammo + 1, self.max_primary))

    def reset(self):
        super().reset()
        self.color = packets.Color()
        self.color.rgb = (112, 112, 112)

    async def set_color(self, r, g, b, *, sender_is_self=False):
        self.color.rgb = r, g, b
        loaders.set_color.player_id = self.connection.id
        loaders.set_color.color.rgb = r, g, b
        predicate = lambda conn: conn != self.connection if sender_is_self else None
        await self.connection.protocol.broadcast_loader(loaders.set_color, predicate)


class Weapon(Tool):
    reload_time = 0
    one_by_one = False

    damage = {HIT.TORSO: None, HIT.HEAD: None, HIT.ARMS: None, HIT.LEGS: None}

    def __init__(self, connection: 'connection.ServerConnection'):
        super().__init__(connection)

        self.reloading: bool = False
        self.reload_call: asyncio.Task = None

    def set_primary(self, primary: bool):
        if (self.primary_ammo <= 0 or self.reloading) and not self.one_by_one:
            self.primary = False
            return False
        if primary and self.one_by_one and self.reloading:
            self.reloading = False
            self.reload_call.cancel()
        self.primary = primary
        return primary

    def set_secondary(self, secondary: bool):
        self.secondary = False  # we dont care about secondary input on most weapons
        return secondary # but we'll relay it to the client anyways

    async def reload(self):
        if self.reloading:
            return False
        if not self.secondary_ammo or self.primary_ammo >= self.max_primary:
            self.reloading = False
            return False

        self.reloading = True
        self.primary = False

        self.reload_call = asyncio.ensure_future(self.on_reload())
        return True

    async def on_reload(self):
        await asyncio.sleep(self.reload_time)
        self.reloading = False
        if not self.one_by_one:
            reserve = max(0, self.secondary_ammo - (self.max_primary - self.primary_ammo))
            self.primary_ammo += self.secondary_ammo - reserve
            self.secondary_ammo = reserve
            await self.send_ammo()
        else:
            self.primary_ammo += 1
            self.secondary_ammo -= 1
            await self.send_ammo()
            await self.reload()

    def on_primary(self):
        if self.primary_ammo <= 0:
            return False
        self.primary_ammo -= 1
        print("SHOOT", self.primary_ammo)
        return True

    def on_secondary(self):
        pass

    def get_damage(self, value):
        if not self.primary or self.reloading:
            return None
        clip_tolerance = int(self.max_primary * 0.3)
        if self.primary_ammo + clip_tolerance <= 0:
            return None
        return self.damage[value]

    async def send_ammo(self):
        loaders.weapon_reload.player_id = self.connection.id
        loaders.weapon_reload.clip_ammo = self.primary_ammo
        loaders.weapon_reload.reserve_ammo = self.secondary_ammo
        await self.connection.send_loader(loaders.weapon_reload)


class Semi(Weapon):
    type = WEAPON.SEMI
    name = "Rifle"

    max_primary = 10
    max_secondary = 50

    primary_rate = 0.5

    reload_time = 2.5
    one_by_one = False

    damage = {HIT.TORSO: 49, HIT.HEAD: 100, HIT.ARMS: 33, HIT.LEGS: 33}


class SMG(Weapon):
    type = WEAPON.SMG
    name = "SMG"

    max_primary = 30
    max_secondary = 120

    primary_rate = 0.11

    reload_time = 2.5
    one_by_one = False

    damage = {HIT.TORSO: 24, HIT.HEAD: 75, HIT.ARMS: 16, HIT.LEGS: 16}


class Shotgun(Weapon):
    type = WEAPON.SHOTGUN
    name = "Shotgun"

    max_primary = 6
    max_secondary = 48

    primary_rate = 1.0

    reload_time = 0.5
    one_by_one = True

    damage = {HIT.TORSO: 21, HIT.HEAD: 24, HIT.ARMS: 14, HIT.LEGS: 14}


class RPG(Weapon):
    type = WEAPON.RPG
    name = "RPG"

    max_primary = 1
    max_secondary = 5

    primary_rate = 1.0

    reload_time = 4.0
    one_by_one = False

    damage = {HIT.TORSO: None, HIT.HEAD: None, HIT.ARMS: None, HIT.LEGS: None}


WEAPONS = {cls.type: cls for cls in Weapon.__subclasses__()}


class Grenade(Tool):
    max_primary = 3

    # max_fuse = 3

    def __init__(self, connection: 'connection.ServerConnection'):
        super().__init__(connection)
        # self.fuse = self.max_fuse

    # over engineered to shit

    # def set_primary(self, primary: bool):
    #     ret = super().set_primary(primary)
    #     if not ret:
    #         asyncio.ensure_future(self.on_primary(self.fuse))
    #     self.fuse = self.max_fuse
    #     return ret
    #
    # async def update(self, dt: float):
    #     if self.primary:
    #         self.fuse -= dt
    #
    # async def on_primary(self, fuse: float):
    #     item: packets.UseOrientedItem = await self.connection.wait_for(packets.UseOrientedItem)
    #     print(fuse, item.value, abs(item.value - fuse))
    #     await self.connection.restock()

    def on_primary(self, *args, **kwargs):
        if self.primary_ammo <= 0:
            return False
        self.primary_ammo -= 1
        return True
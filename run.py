import asyncio
import signal

from aceserver import protocol

try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

loop = asyncio.get_event_loop()
loop.set_debug(True)
server = protocol.ServerProtocol(loop)
try:
    loop.add_signal_handler(signal.SIGINT, server.stop)
    loop.add_signal_handler(signal.SIGTERM, server.stop)
except NotImplementedError:
    pass

try:
    loop.run_until_complete(server.run())
finally:
    server.stop()
    loop.close()

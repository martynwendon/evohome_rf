"""A raw ser2net (local) serial_port to (remote) network relay."""
import asyncio
import logging
from string import printable
from typing import Optional

# timeouts in seconds, 0 means no timeout
RECV_TIMEOUT = 0  # without hearing from client (from network) - not useful
SEND_TIMEOUT = 0  # without hearing from server (from serial port)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.WARNING)


class Ser2NetProtocol(asyncio.Protocol):
    """A TCP socket interface."""

    def __init__(self, cmd_que) -> None:
        _LOGGER.debug("Ser2NetProtocol.__init__(%s)", cmd_que)

        self._cmd_que = cmd_que
        self.transport = None

        if RECV_TIMEOUT:
            self._loop = asyncio.get_running_loop()
            self.timeout_handle = self._loop.call_later(
                RECV_TIMEOUT, self._recv_timeout
            )
        else:
            self._loop = self.timeout_handle = None

    def _recv_timeout(self):
        _LOGGER.debug("Ser2NetProtocol._recv_timeout()")
        self.transport.close()

        _LOGGER.debug(" - socket closed by server (%ss of inactivity).", RECV_TIMEOUT)

    def connection_made(self, transport) -> None:
        _LOGGER.debug("Ser2NetProtocol.connection_made(%s)", transport)

        self.transport = transport
        _LOGGER.debug(" - connection from: %s", transport.get_extra_info("peername"))

    def data_received(self, data) -> None:
        _LOGGER.debug("Ser2NetProtocol.data_received(%s)", data)
        _LOGGER.debug(" - packet received from network: %s", data)

        if self.timeout_handle:
            self.timeout_handle.cancel()
            self.timeout_handle = self._loop.call_later(
                RECV_TIMEOUT, self._recv_timeout
            )

        if data[0] == 0xFF:  # telnet IAC
            # see: https://users.cs.cf.ac.uk/Dave.Marshall/Internet/node141.html
            _LOGGER.debug(" - received a telnet IAC (ignoring): %s", data)
            return

        try:
            packet = "".join(c for c in data.decode().strip() if c in printable)
        except UnicodeDecodeError:
            return

        self._cmd_que.put_nowait(packet)
        _LOGGER.debug(" - command sent to dispatch queue: %s", packet)

    def eof_received(self) -> Optional[bool]:
        _LOGGER.debug("Ser2NetProtocol.eof_received()")
        _LOGGER.debug(" - socket closed by client.")

    def connection_lost(self, exc) -> None:
        _LOGGER.debug("Ser2NetProtocol.connection_lost(%s)", exc)


class Ser2NetServer:
    """A raw ser2net (local) serial_port to (remote) network relay."""

    def __init__(self, addr_port, cmd_que, loop=None) -> None:
        _LOGGER.debug("Ser2NetServer.__init__(%s, %s)", addr_port, cmd_que)

        self._addr, self._port = addr_port.split(":")
        self._cmd_que = cmd_que
        self._loop = loop if loop else asyncio.get_running_loop()
        self.protocol = self.server = None

    def _send_timeout(self):
        _LOGGER.debug("Ser2NetServer._send_timeout()")
        self.protocol.transport.close()

        _LOGGER.debug(" - socket closed by server (%ss of inactivity).", SEND_TIMEOUT)

    async def start(self) -> None:
        _LOGGER.debug("Ser2NetServer.start()")

        self.protocol = Ser2NetProtocol(self._cmd_que)
        self.server = await self._loop.create_server(
            lambda: self.protocol, self._addr, int(self._port)
        )
        asyncio.create_task(self.server.serve_forever())
        _LOGGER.debug(" - listening on %s:%s", self._addr, int(self._port))

    async def write(self, data) -> None:
        _LOGGER.debug("Ser2NetServer.write(%s)", data)

        if self.protocol.transport:
            self.protocol.transport.write(data)
            _LOGGER.debug(" - data sent to network: %s", data)
        else:
            _LOGGER.debug(" - no active network socket, unable to relay")
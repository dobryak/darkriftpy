from __future__ import annotations

import asyncio
import enum
import functools
import logging
import random
from collections.abc import Awaitable, Callable, Generator
from types import TracebackType
from typing import Any, Optional, Protocol, Type, Union, cast

from darkriftpy.client import DarkriftClient
from darkriftpy.io import DarkriftWriter
from darkriftpy.message import DarkriftMessage, MessageType
from darkriftpy.proto import (
    HANDSHAKE_PAYLOAD_LEN,
    Addr,
    DarkriftDatagramProtocol,
    DarkriftStreamProtocol,
    ProtoState,
)

logger = logging.getLogger(__name__)


__all__ = ["DarkriftServer", "serve"]


# Callable type for the server datagram protocol factory
class _DarkriftServerDatagramProtocolFactory(Protocol):
    def __call__(
        self,
        queue_cap: int = 100,
        close_timeout: float = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        addr: Optional[Addr] = None,
    ) -> DarkriftServerDatagramProtocol:
        ...


class ServerState(enum.IntEnum):
    CREATED = 1
    SERVING = 2
    CLOSING = 3
    CLOSED = 4


class DatagramServerTransport(asyncio.DatagramTransport):
    __slots__ = ("_server", "_proto", "_transport", "_closed")

    def __init__(
        self, server: DatagramServer, proto: DarkriftServerDatagramProtocol
    ) -> None:
        """
        :param server: the :class:`~darkriftpy.server.DatagramServer instance
        that will be used as the original transport layer.
        :param proto: the :class:`~darkriftpy.server.DarkriftServerDatagramProtocol`
        instance that is bound with this transport.

        """
        transport = server.get_transport()
        if transport is None:
            raise RuntimeError("The given DatagramServer has no transport attached")

        self._closed = False
        self._server = server
        self._transport = transport
        self._proto = proto

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        return self._transport.get_extra_info(name, default)

    def sendto(
        self, data: bytes, addr: Union[tuple[Any, ...], str, None] = None
    ) -> None:
        assert addr is not None and not self._closed
        self._transport.sendto(data, addr)

    def is_closing(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return

        self._server._close_connection(self._proto)
        self._closed = True

    def abort(self) -> None:
        self.close()


class DarkriftServerStreamProtocol(DarkriftStreamProtocol):
    """
    Server side Darkrift2 protocol handshake implementation (TCP).

    """

    def __init__(
        self,
        server: DarkriftServer,
        queue_cap: int = 100,
        close_timeout: float = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._server = server
        super().__init__(queue_cap, close_timeout, loop)

    async def handshake(self, payload: bytes) -> None:
        """
        Do Darkrift handshake.

        :param payload: handshake payload

        """
        assert self._state == ProtoState.CONNECTING
        assert len(payload) == HANDSHAKE_PAYLOAD_LEN

        await self._write(payload)
        self._connection_ready()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self._server.attach_conn(self)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        super().connection_lost(exc)


class DarkriftServerDatagramProtocol(DarkriftDatagramProtocol):
    """
    Server side Darkrift2 protocol handshake implementation (UDP).

    """

    async def handshake(self) -> None:
        """
        Finish the handshake procedure.

        This protocol is created at the last step of the
        handshake. The last thing we need to do is send an empty datagram.

        """
        assert self._state == ProtoState.CONNECTING

        # The only way to send a datagram with empty payload
        # is using the underlying socket instance directly
        transport = cast(DatagramServerTransport, self._transport)
        socket = transport.get_extra_info("socket")
        if socket is None:
            raise RuntimeError("failed to fetch the underlying socket object")

        if self._paused:
            await self._wait_for_drain()

        self._loop.call_soon(socket.sendto, b"", self._addr)
        self._connection_ready()


class DatagramServer(asyncio.DatagramProtocol):
    def __init__(
        self,
        server: DarkriftServer,
        protocol_factory: _DarkriftServerDatagramProtocolFactory,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if loop is None:
            loop = asyncio.get_event_loop()

        self._paused = False
        self._drain_waiter: Optional[asyncio.Future[None]] = None
        self._drain_lock = asyncio.Lock()

        self._loop = loop
        self._server = server
        self._protocol_factory = protocol_factory
        self._connnections: dict[Addr, DarkriftServerDatagramProtocol] = dict()
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._handshake_waiters: dict[
            bytes, asyncio.Future[DarkriftServerDatagramProtocol]
        ] = dict()

    def get_transport(self) -> Union[asyncio.DatagramTransport, None]:
        return self._transport

    def handshake(
        self, payload: bytes, waiter: asyncio.Future[DarkriftServerDatagramProtocol]
    ) -> None:
        try:
            w = self._handshake_waiters[payload]
            w.cancel()
        except KeyError:
            pass

        self._handshake_waiters[payload] = waiter

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = cast(asyncio.DatagramTransport, transport)

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        conn = self._connnections.get(addr, None)
        if conn:
            conn.datagram_received(data, addr)
            return

        if len(data) != HANDSHAKE_PAYLOAD_LEN:
            logger.warning("Unexpected upd datagram has been recived and dropped...")
            return

        waiters_to_remove = set()
        waiter_found = False
        for k, waiter in self._handshake_waiters.items():
            if waiter.done():
                waiters_to_remove.add(k)
                continue

            if k == data:
                waiter_found = True

        for k in waiters_to_remove:
            del self._handshake_waiters[k]

        if waiter_found:
            self._loop.create_task(self._create_connection(data, addr))
        else:
            logger.warning(
                "There is no handshake waiter for the incoming payload. "
                "Might be a handshake timeout issue"
            )

    async def _create_connection(self, handshake_payload: bytes, addr: Addr) -> None:
        try:
            waiter = self._handshake_waiters[handshake_payload]
            del self._handshake_waiters[handshake_payload]
            proto = self._protocol_factory(addr=addr)

            assert proto.addr is not None

            if self._paused:
                proto.pause_writing()

            proto.connection_made(DatagramServerTransport(self, proto))
            self._connnections[proto.addr] = proto

            await proto.handshake()

            waiter.set_result(proto)
        except KeyError:
            logger.warning("Failed to find a waiter for the given addr")

    def _close_connection(
        self, proto: DarkriftDatagramProtocol, exc: Optional[Exception] = None
    ) -> None:
        try:
            assert proto.addr is not None

            conn = self._connnections[proto.addr]
            del self._connnections[proto.addr]
        except KeyError:
            return

        conn.connection_lost(exc)

    def pause_writing(self) -> None:  # pragma: no cover
        assert not self._paused
        self._paused = True

        for conn in self._connnections.values():
            conn.pause_writing()

    def resume_writing(self) -> None:  # pragma: no cover
        assert self._paused
        self._paused = False

        for conn in self._connnections.values():
            conn.resume_writing()

    def connection_lost(self, exc: Optional[Exception]) -> None:
        for conn in self._connnections.values():
            conn.connection_lost(exc)

        self._connnections = dict()

    def error_received(self, exc: Exception) -> None:
        for conn in self._connnections.values():
            conn.error_received(exc)


class DarkriftServer:
    """
    Darkrift2 server.

    The ``conn_handler`` argument is a callable that is used to handle a new
    incoming connection.

    The ``queue_cap`` argument specifies the maximum capacity of the message
    queue for each client.

    The ``close_timeout`` argument specifies the maximum wait time for closing
    the connection. (the actual wait time may be greater than the specified
    one)

    """

    def __init__(
        self,
        conn_handler: Callable[[DarkriftClient], Awaitable[Any]],
        queue_cap: int = 100,
        close_timeout: float = 5,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        if loop is None:
            loop = asyncio.get_event_loop()

        self._loop = loop
        self._connection_counter = 0
        self._state = ServerState.CREATED
        self._conn_handler = conn_handler
        self._clients: set[DarkriftClient] = set()
        self._conn_handler_tasks: dict[DarkriftClient, asyncio.Task[None]] = dict()
        self._close_timeout = close_timeout
        self._tcp_server: Optional[asyncio.AbstractServer] = None
        self._udp_server: Optional[DatagramServer] = None
        self._close_task: Optional[asyncio.Task[None]] = None
        self._close_waiter = loop.create_future()

        self._create_client = functools.partial(
            DarkriftClient, queue_cap=queue_cap, close_timeout=close_timeout, loop=loop
        )

    @property
    def is_serving(self) -> bool:
        return self._state is ServerState.SERVING

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def attach_server(
        self, tcp_server: asyncio.AbstractServer, udp_server: DatagramServer
    ) -> None:
        """
        Attach the given tcp :class:`asyncio.AbstractServer` server and
        :class:`DatagramServer` udp endpoint.

        :param tcp_server: an instance of :class:`asyncio.AbstractServer`
        :param udp_server: an instance of :class:`~darkriftpy.server.DatagramServer`

        :raises RuntimeError: if the server already has an attached tcp server
        and udp endpoint and currently is in the serving state.

        """
        if self.is_serving:
            raise RuntimeError(
                f"failed to attach a tcp server. "
                f"Server {self!r} is alrady serving the attached one"
            )

        self._tcp_server = tcp_server
        self._udp_server = udp_server

        if self._tcp_server.is_serving():
            self._state = ServerState.SERVING

    def close(self) -> None:
        if self._state is not ServerState.SERVING:
            return

        self._state = ServerState.CLOSING

        if self._close_task is None:
            self._close_task = self._loop.create_task(self._close())

    async def _close(self) -> None:
        assert self._tcp_server is not None

        try:
            self._tcp_server.close()
            await self._tcp_server.wait_closed()

            # Iterate the event loop
            await asyncio.sleep(0)

            if self._clients:
                for client in self._clients:
                    client.close()

                await asyncio.wait(
                    [
                        asyncio.create_task(client.wait_closed())
                        for client in self._clients
                    ]
                    + [task for task in self._conn_handler_tasks.values()]
                )

        finally:
            self._close_waiter.set_result(None)
            self._state = ServerState.CLOSED

    async def wait_closed(self) -> None:
        await asyncio.shield(self._close_waiter)

    async def _handle_connection(self, tcp_proto: DarkriftServerStreamProtocol) -> None:
        """
        Handle the given tcp connection.

        This coroutine first tries to establish the darkrift handshake with
        the given tcp connection and if successful passes the created
        :class:`~darkrift.client.DarkriftClient` instance to the user's
        connection handler callback.

        :param tcp_proto: incoming tcp connection.

        """
        client = await self._handshake(tcp_proto)
        if client is None:
            logger.warning("the handshake failed. Drop connection")
            tcp_proto.close()
            await tcp_proto.wait_closed()
            return

        current_task = asyncio.current_task(self._loop)
        assert current_task is not None

        self._clients.add(client)
        self._conn_handler_tasks[client] = current_task

        w = DarkriftWriter()
        w.write_uint16(client.connection_id)

        try:
            try:
                await client.send(
                    DarkriftMessage(0, w.bytes, MessageType.CONNECTION_ID), True
                )
            except Exception:
                logger.error(
                    "failed to send connection id to the client", exc_info=True
                )

            try:
                await self._conn_handler(client)
            except Exception:
                logger.error(
                    "exception received while handle the clinet connection",
                    exc_info=True,
                )

        finally:
            client.close()
            await client.wait_closed()

            try:
                self._clients.remove(client)
            except KeyError:
                logger.warning(
                    "failed to remove the client. There is not such client "
                    "in the client set"
                )

            try:
                del self._conn_handler_tasks[client]
            except KeyError:
                logger.warning(
                    "failed to remove the connection handler task, "
                    "becasue the handler task was not found."
                )

    async def _handshake(
        self, tcp_proto: DarkriftServerStreamProtocol
    ) -> Union[DarkriftClient, None]:
        """
        Do the darkrift handshake and if successful create DarkriftClient.

        :param tcp_proto: incoming tcp connection

        :returns: :class:`~darkriftpy.client.DarkriftClient` if the handshake
        was successful, otherwise returns :class:`None`

        """

        assert self._udp_server is not None

        # TODO: create a safer way to generate a handshake payload.
        # There is a very small chance to generate a duplicate handshake payload.
        # If this happens the previous connection with the same handshake payload
        # will be droped (if it is still in the handshake state).
        handshake_payload = random.randbytes(HANDSHAKE_PAYLOAD_LEN)
        waiter = self._loop.create_future()
        self._udp_server.handshake(handshake_payload, waiter)

        await tcp_proto.handshake(handshake_payload)

        try:
            await asyncio.wait_for(waiter, 5)
        except asyncio.TimeoutError:
            return None

        udp_proto = waiter.result()

        client = self._create_client(tcp_proto=tcp_proto, udp_proto=udp_proto)
        client.connection_id = self._get_next_connection_id()

        if __debug__:
            logger.debug(f"created a new client with id: {client.connection_id}")

        return client

    def _get_next_connection_id(self) -> int:
        self._connection_counter += 1
        return self._connection_counter

    def attach_conn(self, tcp_proto: DarkriftServerStreamProtocol) -> None:
        self._loop.create_task(self._handle_connection(tcp_proto))


class Serve:
    """
    Start serving Darkrift2 clients on the specified host and ports.

    :func:`~darkrift.server.serve` can also be used as async context manager::

        async with serve(handle_client) as server:
            ...

    The ``conn_handler`` argument is a callable that is used to handle a new
    incoming connection.

    The ``host`` argument specifies a host that will be using to bind the
    socket to.

    The ``tcp_port`` argument specifies the bind port for the tcp socket.

    The ``udp_port`` argument specifies the bind port for the udp socket.

    The ``queue_cap`` argument specifies the maximum capacity of the message
    queue for each client.

    The ``backlog`` argument specifies the maximum number of incomming (not
    accepted) connections in the internal accepting queue.

    """

    def __init__(
        self,
        conn_handler: Callable[[DarkriftClient], Awaitable[Any]],
        host: str = "127.0.0.1",
        tcp_port: int = 4296,
        udp_port: int = 4296,
        queue_cap: int = 100,
        close_timeout: float = 5,
        backlog: int = 50,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        if loop is None:
            loop = asyncio.get_event_loop()

        self._loop = loop
        self._server = DarkriftServer(conn_handler, queue_cap, close_timeout, loop)
        self._conn_handler = conn_handler
        self._host = host
        self._close_timeout = close_timeout
        self._backlog = backlog

        self._create_tcp_server = functools.partial(
            loop.create_server,
            protocol_factory=functools.partial(
                DarkriftServerStreamProtocol,
                server=self._server,
                queue_cap=queue_cap,
                close_timeout=close_timeout,
                loop=loop,
            ),
            host=host,
            port=tcp_port,
            backlog=backlog,
        )

        self._create_udp_server = functools.partial(
            loop.create_datagram_endpoint,
            protocol_factory=functools.partial(
                DatagramServer,
                server=self._server,
                protocol_factory=functools.partial(
                    DarkriftServerDatagramProtocol,
                    queue_cap=queue_cap,
                    close_timeout=close_timeout,
                    loop=loop,
                ),
                loop=loop,
            ),
            local_addr=(host, udp_port),
        )

    def __await__(self) -> Generator[Any, None, DarkriftServer]:
        return self.__await__impl_().__await__()

    async def __await__impl_(self) -> DarkriftServer:
        """
        :raises OSError: when failed to bind to the given address and port

        """
        try:
            _, udp_server = await self._create_udp_server()
        except Exception:
            raise OSError("failed to create an instance of the udp server")

        udp_server = cast(DatagramServer, udp_server)
        tcp_server = await self._create_tcp_server()

        self._server.attach_server(tcp_server, udp_server)
        return self._server

    async def __aenter__(self) -> DarkriftServer:
        return await self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self._server.close()
        await self._server.wait_closed()


serve = Serve

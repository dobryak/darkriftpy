from __future__ import annotations

import asyncio
import collections
import enum
import functools
import logging
from collections.abc import AsyncIterator, Generator
from types import TracebackType
from typing import Any, Deque, Optional, cast

from darkriftpy.exceptions import (
    ConnectionClosedError,
    ConnectionTimeoutError,
    HandshakeError,
    ProtocolError,
)
from darkriftpy.message import DarkriftMessage, MessageType
from darkriftpy.proto import (
    HANDSHAKE_PAYLOAD_LEN,
    DarkriftDatagramProtocol,
    DarkriftProtocol,
    DarkriftStreamProtocol,
    ProtoState,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DarkriftClient",
    "connect",
]


class ClientState(enum.IntEnum):
    CONNECTED = 1
    CLOSING = 2
    CLOSED = 3


class DarkriftClientStreamProtocol(DarkriftStreamProtocol):
    """
    Client side Darkrift2 protocol handshake implementation (TCP).

    """

    def __init__(
        self,
        queue_cap: int = 100,
        close_timeout: int = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._handshake_payload = bytearray()
        self._handshake_payload_waiter: Optional[asyncio.Future[None]] = None
        super().__init__(queue_cap, close_timeout, loop)

    async def handshake(self, proto: DarkriftClientDatagramProtocol) -> None:
        """
        Do Darkrift handshake.

        :param proto: the udp endpoint that should be associated with the
        current connection.

        :raises ~darkriftpy.exceptions.ProtocolError: when wrong handshake
        payload received.
        :raises ~darkriftpy.exceptions.HandshakeError: when the handshake
        failed for some reason.

        """
        assert self._state == ProtoState.CONNECTING

        if len(self._handshake_payload) != HANDSHAKE_PAYLOAD_LEN:
            waiter = self._loop.create_future()
            self._handshake_payload_waiter = waiter
            try:
                done, panding = await asyncio.wait(
                    [waiter], timeout=5, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                self._handshake_payload_waiter = None

            if not waiter.done():
                raise HandshakeError()

            if len(self._handshake_payload) != HANDSHAKE_PAYLOAD_LEN:
                raise ProtocolError("Received wrong handshake payload")

        self._connection_ready()
        await proto.handshake(self._handshake_payload)

    def data_received(self, data: bytes) -> None:
        if self._state == ProtoState.CONNECTED:
            super().data_received(data)
            return

        if self._state == ProtoState.CONNECTING:
            self._handshake_payload.extend(data)

            if len(self._handshake_payload) != HANDSHAKE_PAYLOAD_LEN:
                # wait for other pieces of the handshake payload
                return

            if self._handshake_payload_waiter is not None:
                self._handshake_payload_waiter.set_result(None)
                self._handshake_payload_waiter = None


class DarkriftClientDatagramProtocol(DarkriftDatagramProtocol):
    """
    Client side Darkrift2 protocol handshake implementation (UDP).

    """

    def __init__(
        self,
        queue_cap: int = 100,
        close_timeout: int = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self._handshake_waiter: Optional[asyncio.Future[None]] = None
        super().__init__(queue_cap, close_timeout, loop)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if self._state == ProtoState.CONNECTED:
            super().datagram_received(data, addr)
            return

        if self._state == ProtoState.CONNECTING:
            exc = None
            if len(data) != 0:
                exc = HandshakeError("Received wrong handhshake datagram")
            else:
                self._connection_ready()

            if self._handshake_waiter is not None:
                if exc is None:
                    self._handshake_waiter.set_result(None)
                else:
                    self._handshake_waiter.set_exception(exc)

    async def handshake(self, handshake_payload: bytes) -> None:
        assert self._state == ProtoState.CONNECTING

        waiter = self._loop.create_future()
        self._handshake_waiter = waiter

        transport = cast(asyncio.DatagramTransport, self._transport)
        transport.sendto(handshake_payload)

        try:
            await asyncio.wait_for(waiter, 5)
        except asyncio.TimeoutError:
            raise HandshakeError() from None
        finally:
            self._handshake_waiter = None

        exc = waiter.exception()
        if exc is not None:
            raise exc


class DarkriftClient:
    """
    Darkrift2 client.

    :class:`~darkriftpy.client.DarkriftClient` supports async iteration::

        async for message in client:
            await handle_message(message)

    The ``tcp_proto`` argument is a TCP connection with the completed
    handshake.

    The ``udp_proto`` argument is a UDP pseudo-connection with the completed
    handshake.

    The ``queue_cap`` argument specifies the maximum capacity of the message queue.

    .. note::
        If the message queue is full, it may cause messages to be dropped if
        they are comming from the udp endpoint and the underlygin queue of the
        udp protocol is also full.

    The ``close_timeout`` argument specifies the maximum wait time for the
    closing connection.

    .. note::
        The actual time to wait until the connection is closed may be greater
        than the given one. See protocol specific implementation.

    """

    def __init__(
        self,
        tcp_proto: DarkriftStreamProtocol,
        udp_proto: DarkriftDatagramProtocol,
        queue_cap: int = 100,
        close_timeout: float = 5,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """
        :raises ~darkriftpy.exceptions.ConnectionClosedError: when one or both
        the given protocols have connenction closed state.

        """
        if not tcp_proto.is_connected() or not udp_proto.is_connected():
            raise ConnectionClosedError()

        if loop is None:
            loop = asyncio.get_event_loop()

        self._loop = loop
        self._tcp = tcp_proto
        self._udp = udp_proto
        self._close_timeout = close_timeout

        self.queue_cap = queue_cap
        self._messages: Deque[DarkriftMessage] = collections.deque()

        self._state = ClientState.CONNECTED
        self._connection_id = 0

        self._append_message_waiter: Optional[asyncio.Future[None]] = None
        self._pop_message_waiter: Optional[asyncio.Future[None]] = None

        self._tcp_recv_task = loop.create_task(self._proto_recv(tcp_proto))
        self._udp_recv_task = loop.create_task(self._proto_recv(udp_proto))
        self._close_connection_task = loop.create_task(self._close_connection())

    @property
    def connection_id(self) -> int:
        """
        Connection identifier of the client assigned by the server.

        Getter

        """
        return self._connection_id

    @connection_id.setter
    def connection_id(self, connection_id: int) -> None:
        """
        Set the connection identifier for the current client.

        """
        self._connection_id = connection_id

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    async def send(self, message: DarkriftMessage, reliable: bool = True) -> None:
        """
        Send the given message to the Darkrift2 server.

        :param message: the message to send.
        :param reliable: if ``True`` the given message will be sent using the
        tcp transport otherwise the udp transport will be used for sending the
        message.

        :raises ~darkriftpy.exceptions.ConnectionClosedError: if the
        transport is not connected.

        """
        await (self._tcp if reliable else self._udp).send(message)

    async def recv(self) -> DarkriftMessage:
        """
        Receive the next message.

        :raises ~darkriftpy.exceptions.ConnectionClosedError: when disconnected
        from the server.

        """
        if self._pop_message_waiter is not None:
            raise RuntimeError(
                "It is not allowed to call recv while another "
                "coroutine is already awaiting the recv"
            )

        while len(self._messages) <= 0 and self._state == ClientState.CONNECTED:
            waiter: asyncio.Future[None] = self._loop.create_future()
            self._pop_message_waiter = waiter

            try:
                await asyncio.wait(
                    [waiter, self._tcp_recv_task, self._udp_recv_task],
                    loop=self._loop,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                self._pop_message_waiter = None

            if not waiter.done():
                raise ConnectionClosedError()

        try:
            message = self._messages.popleft()
        except IndexError:
            raise ConnectionClosedError()

        if self._append_message_waiter is not None:
            self._append_message_waiter.set_result(None)
            self._append_message_waiter = None

        return message

    def __aiter__(self) -> AsyncIterator[DarkriftMessage]:
        return self

    async def __anext__(self) -> DarkriftMessage:
        """
        Returns the next message.

        Stop iteration when the connection is closed.

        """
        try:
            return await self.recv()
        except ConnectionClosedError:
            raise StopAsyncIteration()

    def close(self) -> None:
        if __debug__:
            logger.debug(f"{self!r} close connection")

        if self._state in (ClientState.CLOSING, ClientState.CLOSED):
            return

        self._state = ClientState.CLOSING

        self._tcp.close()
        self._udp.close()

    async def wait_closed(self) -> None:
        """
        Wait until the both of connections are closed.

        """
        await asyncio.shield(self._close_connection_task)

    async def _close_connection(self) -> None:
        """
        Close the connection if at least one of the receiving tasks finishes.

        """
        try:
            if all((self._tcp_recv_task is not None, self._udp_recv_task is not None)):
                done, panding = await asyncio.wait(
                    [self._tcp_recv_task, self._udp_recv_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

            if self._tcp_recv_task is not None and not self._tcp_recv_task.done():
                self._tcp_recv_task.cancel()

            if self._udp_recv_task is not None and not self._udp_recv_task.done():
                self._udp_recv_task.cancel()

        finally:
            self._tcp.close()
            self._udp.close()

            await self._tcp.wait_closed()
            await self._udp.wait_closed()

            self._state = ClientState.CLOSED

    async def _proto_recv(self, proto: DarkriftProtocol) -> None:
        """
        Receive the next message from the given protocol.

        The received message is put into the internal message queue.

        """
        if self._state is not ClientState.CONNECTED:
            return

        try:
            while True:
                message = await proto.recv()
                if message.type is not MessageType.USER:
                    self._process_ctrl_message(message)
                    continue

                while len(self._messages) >= self.queue_cap:
                    if self._append_message_waiter is None:
                        self._append_message_waiter = self._loop.create_future()

                    try:
                        await asyncio.shield(self._append_message_waiter)
                    finally:
                        self._append_message_waiter = None

                self._messages.append(message)

                if self._pop_message_waiter is not None:
                    self._pop_message_waiter.set_result(None)
                    self._pop_message_waiter = None

        except ConnectionClosedError:
            pass

        except Exception:
            logger.error("unexpected exception in a proto recv task", exc_info=True)

    def _process_ctrl_message(self, msg: DarkriftMessage) -> None:
        """
        Process the system message.

        We treat the message as a system one if its type is not:
        `~darkriftpy.proto.MessageType.USER`

        """
        if msg.type is MessageType.CONNECTION_ID:
            r = msg.get_reader()
            self.connection_id = r.read_uint16()
            if __debug__:
                logger.debug(f"{self!r} connection id: {self._connection_id}")


class Connect:
    """
    Connect to the Darkrift2 server at the given endpoint.

    After successful connect the hanshake is made and a new instance of
    :class:`~darkriftpy.client.DarkriftClient` is created.

    of :class:`~darkriftpy.client.DarkriftClient`

    The instance of this class is awaitable::

        client = await Connect("127.0.0.1")

    For convenience, use :func:`~darkriftpy.client.connect`::

        client = await connect("127.0.0.1")

    :func:`~darkriftpy.client.connect` can also be used as async context manager::

        async with connect("127.0.0.1") as client:
            async for message in client:
                await handle_message(message)

    The ``host`` argument specifies the host address of the Darkrift2 server.

    The ``tcp_port`` argument specifies the tcp port of the Darkrift2 server.

    The ``udp_port`` argument specifies the UDP port of the Darkrift2 server.

    The ``queue_cap`` argument specifies the maximum capacity of the message queue.

    .. note::
        If the message queue is full, it may cause messages to be dropped if
        they are comming from the udp endpoint and the underlygin queue of the
        udp protocol is also full.

    The ``conn_timeout`` argument specifies the maximum time to wait for the
    connection and hanshake.

    The ``close_timeout`` argument specifies the maximum wait time for the
    closing connection.

    .. note::
        The actual time to wait until the connection is closed may be greater
        than the given one. See protocol specific implementation.

    """

    def __init__(
        self,
        host: str,
        tcp_port: int = 4296,
        udp_port: int = 4296,
        queue_cap: int = 100,
        conn_timeout: int = 30,
        close_timeout: float = 5,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        if loop is None:
            loop = asyncio.get_event_loop()

        self._loop = loop
        self._conn_timeout = conn_timeout
        self._queue_cap = queue_cap
        self._close_timeout = close_timeout
        self._host = host
        self._tcp_port = tcp_port
        self._udp_port = udp_port
        self._client: Optional[DarkriftClient] = None

        stream_proto_factory = functools.partial(
            DarkriftClientStreamProtocol,
            queue_cap=queue_cap,
            close_timeout=close_timeout,
            loop=loop,
        )

        datagram_proto_factory = functools.partial(
            DarkriftClientDatagramProtocol,
            queue_cap=queue_cap,
            close_timeout=close_timeout,
            loop=loop,
        )

        self._create_tcp_connection = functools.partial(
            loop.create_connection,
            stream_proto_factory,
            host,
            tcp_port,
        )

        self._create_udp_endpoint = functools.partial(
            loop.create_datagram_endpoint,
            datagram_proto_factory,
            remote_addr=(host, udp_port),
        )

    def __await__(self) -> Generator[Any, None, DarkriftClient]:
        return self.__await_impl__().__await__()

    async def __await_impl__(self) -> DarkriftClient:
        """
        Connect to the Darkrift2 server.

        :returns: :class:~darkriftpy.client.DarkriftClient

        :raises ~darkriftpy.exceptions.ConnectionTimeoutError: when failed to
        connect to the given endpoint within the given connection timeout.

        :raises ConnectionError: when tcp connection to the given endpoint failed.

        """
        try:
            _, udp_proto = await self._create_udp_endpoint()
        except Exception:
            raise OSError("failed to create the udp endpoint") from None

        try:
            _, tcp_proto = await asyncio.wait_for(
                self._create_tcp_connection(), self._conn_timeout
            )
        except asyncio.TimeoutError:
            raise ConnectionTimeoutError(
                f"Connection to {self._host}:{self._tcp_port} timeout"
            ) from None

        tcp_proto = cast(DarkriftClientStreamProtocol, tcp_proto)
        udp_proto = cast(DarkriftClientDatagramProtocol, udp_proto)

        try:
            await tcp_proto.handshake(udp_proto)
        except Exception as exc:
            tcp_proto.close()
            udp_proto.close()

            await tcp_proto.wait_closed()
            await udp_proto.wait_closed()

            logger.error("handshake failed", exc_info=True)

            raise HandshakeError(
                "Faield to establish a handshake with the server"
            ) from exc

        self._client = DarkriftClient(
            tcp_proto, udp_proto, self._queue_cap, self._close_timeout, self._loop
        )

        return self._client

    async def __aenter__(self) -> DarkriftClient:
        return await self

    async def __aexit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc_value: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        if self._client:
            self._client.close()
            await self._client.wait_closed()


connect = Connect

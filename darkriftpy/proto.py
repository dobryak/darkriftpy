from __future__ import annotations

import asyncio
import collections
import enum
import logging
from abc import ABC, abstractmethod
from typing import Deque, Optional, Union, cast

from darkriftpy.exceptions import ConnectionClosedError
from darkriftpy.io import DarkriftReader, DarkriftWriter
from darkriftpy.message import DarkriftMessage, MessageType

logger = logging.getLogger(__name__)


HANDSHAKE_PAYLOAD_LEN = 9
Addr = tuple[str, int]


class ProtoState(enum.IntEnum):
    CONNECTING = 1
    CONNECTED = 2
    CLOSING = 3
    CLOSED = 4


class DarkriftProtocol(ABC):
    """
    Base Darkrift2 protocol implementation for both TCP and UDP transports.

    The ``queue_cap`` argument sets the maximum capacity of the queue with
    incoming messages

    The ``close_timeout`` argument specifies the maximum wait time in seconds
    for closing the connection.

    .. note::
        The actual time to wait for closing the connection may be greater than
        the specified value. See :meth:`close` of the concrete protocol for
        details

    """

    def __init__(
        self,
        queue_cap: int = 100,
        close_timeout: float = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if loop is None:
            loop = asyncio.get_event_loop()

        self._close_timeout = close_timeout

        self._messages: Deque[DarkriftMessage] = collections.deque()
        self.queue_cap = queue_cap
        self._loop = loop

        self._paused = False
        self._drain_waiter: Optional[asyncio.Future[None]] = None
        self._drain_lock = asyncio.Lock()

        self._state = ProtoState.CONNECTING
        self._append_message_waiter: Optional[asyncio.Future[None]] = None
        self._pop_message_waiter: Optional[asyncio.Future[None]] = None
        self._connection_lost_waiter: asyncio.Future[None] = loop.create_future()
        self._transport: Optional[asyncio.BaseTransport] = None

    @property
    def addr(self) -> Union[Addr, None]:
        """
        Remote address of the current connection.

        """
        if self._state is not ProtoState.CONNECTED:
            return None

        transport = cast(asyncio.BaseTransport, self._transport)
        addr = transport.get_extra_info("peername")
        if addr is None:
            return addr

        return cast(Addr, addr)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    def is_connected(self) -> bool:
        """
        ``True`` if the connection is active.

        This means that the hanshake has been completed and the underlying
        connection is ready for incoming and outcoming data.

        """
        return self._state is ProtoState.CONNECTED

    async def _wait_for_drain(self, timeout: float = 0) -> None:
        """
        Wait until the underline write buffer is drained.

        Returns immediately if writing is not paused for now.

        :param timeout: the maximum number of seconds to wait for.
        0 means wait forever.

        :raises ~asyncio.TimeoutError: if waiting is timed out

        :returns: None

        """
        if not self._paused:
            return

        async with self._drain_lock:
            # check if still paused
            if not self._paused:
                return

            waiter = self._drain_waiter
            assert waiter is None or waiter.cancelled()
            waiter = self._loop.create_future()
            self._drain_waiter = waiter

            if timeout > 0:
                await asyncio.wait_for(waiter, timeout)
            else:
                await waiter

    async def _write(self, b: bytes) -> None:
        """
        Write given bytes to the underlying transport.

        :param b: bytes to send

        """
        if self._paused:
            await self._wait_for_drain()

        self._write_internal(b)

    @abstractmethod
    def _write_internal(self, b: bytes) -> None:
        ...

    async def send(self, msg: DarkriftMessage) -> None:
        """
        Send the given :class:`~darkriftpy.proto.DarkriftMessage` message.

        :param msg: :class:`~darkriftpy.proto.DarkriftMessage` message to send

        :raises :exc:`~darkriftpy.exceptions.ConnectionClosedError` if the
        underlying transport is not connected.

        """
        if self._state is not ProtoState.CONNECTED:
            raise ConnectionClosedError(
                "Failed to write the given message. Connection is closed"
            )

        await self._write(self._encode_message(msg))

    @abstractmethod
    def close(self) -> None:
        """
        Close the underlying transport.

        """
        ...

    async def wait_closed(self) -> None:
        """
        Wait until the underlying transport is closed

        """
        await asyncio.shield(self._connection_lost_waiter)

    async def recv(self) -> DarkriftMessage:
        """
        Receive the next message.

        :raises RuntimeError: if :meth:`recv` is alrady awaited by another coroutine
        :raises ConnectionClosedError: when connection is closed

        :returns: a :class:`~darkriftpy.DarkriftMessage`

        """
        if self._pop_message_waiter is not None:
            raise RuntimeError(
                "It is not allowed to call recv while another "
                "coroutine is already awaiting the recv"
            )

        while len(self._messages) <= 0:
            waiter: asyncio.Future[None] = self._loop.create_future()
            self._pop_message_waiter = waiter

            try:
                await asyncio.wait(
                    [waiter, self._connection_lost_waiter],
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                self._pop_message_waiter = None

            if not waiter.done():
                if __debug__:
                    logger.debug(
                        f"{self!r} connection has been closed. "
                        "Raise connection closed error"
                    )

                raise ConnectionClosedError()

        message = self._messages.popleft()

        if self._append_message_waiter is not None:
            self._append_message_waiter.set_result(None)
            self._append_message_waiter = None

        return message

    def _decode_message(self, reader: DarkriftReader) -> DarkriftMessage:
        """
        Create :class:`~darkriftpy.proto.DarkriftMessage` from given bytes.

        :param reader: binery reader with the message payload

        :raises :exc:`RuntimeError`: if failed to decode the message
        from the given payload

        """
        try:
            msg_type = reader.read_uint8()
            try:
                msg_type = MessageType(msg_type)
            except ValueError:
                logger.error(
                    f"Received unknown message type: {msg_type}. "
                    f"MessageType.UNKNOWN message type is used as fallback"
                )
                msg_type = MessageType.UNKNOWN

            tag = reader.read_int16()

            return DarkriftMessage(tag, reader.read(), msg_type)
        except Exception:
            raise RuntimeError("failed to decode the message")

    def _encode_message(self, msg: DarkriftMessage) -> bytes:
        """
        Encode the given message to bytes.

        :param msg: the message to encode

        """
        w = DarkriftWriter()
        w.write_uint8(int(msg.type))
        w.write_int16(msg.tag)

        return w.bytes + msg.bytes

    def _connection_ready(self) -> None:
        """
        It is called when the opening handshake completes.

        """

        self._state = ProtoState.CONNECTED

    async def _wait_connection_lost(self, timeout: float) -> bool:
        """
        Wait for connection lost with the specified timeout.

        :param timeout: seconds to wait for connection lost.

        :returns: True if the connection is lost, otherwise
        returns False.

        """
        if not self._connection_lost_waiter.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._connection_lost_waiter), timeout
                )
            except asyncio.TimeoutError:
                pass

        return self._connection_lost_waiter.done()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport

    def pause_writing(self) -> None:  # pragma: no cover
        assert not self._paused
        self._paused = True

    def resume_writing(self) -> None:  # pragma: no cover
        assert self._paused
        self._paused = False

        waiter = self._drain_waiter
        if waiter is not None:
            self._drain_waiter = None
            if not waiter.done():
                waiter.set_result(None)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        if __debug__:
            logger.debug(f"{self!r} connection lost")

        self._state = ProtoState.CLOSED

        if exc is None:
            self._connection_lost_waiter.set_result(None)
        else:
            self._connection_lost_waiter.set_exception(exc)

        if not self._paused:
            return

        waiter = self._drain_waiter
        if waiter is None:
            return

        self._drain_waiter = None
        if waiter.done():
            return

        if exc is None:
            waiter.set_result(None)
        else:
            waiter.set_exception(exc)


class DarkriftStreamProtocol(DarkriftProtocol, asyncio.Protocol):
    """
    Darkrift2 protocol implementation for a TCP connection.

    The ``close_timeout`` argument specifies the maximum wait time for closing
    the connection. This time actually can be up to ``3 * close_timeout``, see
    :meth:`_connection_close`.

    """

    def __init__(
        self,
        queue_cap: int = 100,
        close_timeout: float = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        super().__init__(queue_cap, close_timeout, loop)
        self._reader = asyncio.StreamReader(limit=2 ** 16, loop=loop)
        self._data_consume_task: Optional[asyncio.Task[None]] = None
        self._connection_close_task: Optional[asyncio.Task[None]] = None

    async def _connection_close(self) -> None:
        """
        Connection close task.

        """
        if __debug__:
            logger.debug(f"{self!r} connection close task")

        if self._data_consume_task is not None:
            try:
                await self._data_consume_task
            except asyncio.CancelledError:
                pass

        try:
            if (
                isinstance(self._transport, asyncio.WriteTransport)
                and self._transport.can_write_eof()
            ):
                self._transport.write_eof()

        except NotImplementedError:
            # isinstance(self._transport, asyncio.WriteTransport)
            # for a datagram transport returns true
            # hasattr(self._transport, "can_write_eof") for a
            # datagram transport returns true
            pass

        if await self._wait_connection_lost(self._close_timeout):
            return

        transport = cast(asyncio.Transport, self._transport)

        transport.close()

        if await self._wait_connection_lost(self._close_timeout):
            return

        if __debug__:
            logger.debug(f"{self!r} transport abort the connection")

        transport.abort()

        await self._wait_connection_lost(self._close_timeout)

    def close(self) -> None:
        """
        Start the connection closing procedure.

        This method cancels the consume task if it is running.

        """
        if __debug__:
            logger.debug(f"{self!r} close connection")

        if self._state in (ProtoState.CLOSED, ProtoState.CLOSING):
            return

        self._state = ProtoState.CLOSING

        transport = cast(asyncio.Transport, self._transport)
        if transport.is_closing():
            return

        if self._connection_close_task is None:
            self._loop.create_task(self._connection_close())

        if self._data_consume_task is not None and not self._data_consume_task.done():
            if __debug__:
                logger.debug(f"{self!r} cancel data consume task")
            self._data_consume_task.cancel()

    async def _consume_data(self) -> None:
        """
        Read incoming messages in loop and put them in the message queue.

        :meth:`_connection_ready` starts this coroutine in a task.

        """
        try:
            while True:
                message = await self._read_message()
                while len(self._messages) >= self.queue_cap:
                    self._append_message_waiter = self._loop.create_future()
                    try:
                        await asyncio.shield(self._append_message_waiter)
                    finally:
                        self._append_message_waiter = None

                self._messages.append(message)

                if self._pop_message_waiter is not None:
                    self._pop_message_waiter.set_result(None)
                    self._pop_message_waiter = None

        except asyncio.CancelledError:
            pass

        except (ConnectionError, EOFError):
            pass

        except Exception:
            logger.error("unexpected exception in data consuming task", exc_info=True)
            pass

    def _connection_ready(self) -> None:
        """
        Called after the handshake completes.

        Run the main data consume task :meth:`_consume_data` and connection
        close task :meth:`_connection_close` that monitors the state of the forehead.

        It is called when opening handshake completes.

        """
        super()._connection_ready()

        self._data_consume_task = self._loop.create_task(self._consume_data())
        self._connection_close_task = self._loop.create_task(self._connection_close())

    def _write_internal(self, b: bytes) -> None:
        transport = cast(asyncio.Transport, self._transport)
        transport.write(b)

    def _encode_message(self, msg: DarkriftMessage) -> bytes:
        w = DarkriftWriter()
        w.write_int32(len(msg.bytes) + 3)
        w.write_uint8(int(msg.type))
        w.write_int16(msg.tag)

        return w.bytes + msg.bytes

    async def _read_message(self) -> DarkriftMessage:
        """
        Read and parse the next message from the reader.

        :raises ConnectionError: if the underlying connection breaks
        :raises EOFError: if not enough bytes available for reading.
        This may cause EOF state of the underlying connection
        :raise RuntimeError: if failed to parse the stream.

        :returns: :class:`~darkriftpy.DarkriftMessage`

        """
        msg_len = DarkriftReader(await self._reader.readexactly(4)).read_int32()

        return self._decode_message(
            DarkriftReader(await self._reader.readexactly(msg_len))
        )

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        super().connection_made(transport)
        self._reader.set_transport(transport)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        super().connection_lost(exc)

        self._reader.feed_eof()

    def data_received(self, data: bytes) -> None:
        if self._reader is not None:
            self._reader.feed_data(data)
            return

    def eof_received(self) -> None:
        self._reader.feed_eof()


class DarkriftDatagramProtocol(DarkriftProtocol, asyncio.DatagramProtocol):
    """
    Darkrift2 protocol implementation for a UDP pseudo-connection.

    The ``addr`` argument specifies the remote addr that will be used to send
    messages to. If ``None`` outcoming messages will be sent to the address
    specified in :func:`~loop.create_datagram_endpoint`

    """

    def __init__(
        self,
        queue_cap: int = 100,
        close_timeout: int = 30,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        addr: Optional[Addr] = None,
    ) -> None:
        self._addr = addr
        super().__init__(queue_cap, close_timeout, loop)

    @property
    def addr(self) -> Union[Addr, None]:
        if self._addr is not None:
            return self._addr

        return super().addr

    def close(self) -> None:
        """
        Close the underlying transport.

        """
        if __debug__:
            logger.debug(f"{self!r} close connection")

        if self._state in (ProtoState.CLOSED, ProtoState.CLOSING):
            return

        self._state = ProtoState.CLOSING

        transport = cast(asyncio.DatagramTransport, self._transport)
        if transport.is_closing():
            return

        transport.close()

    def _write_internal(self, b: bytes) -> None:
        transport = cast(asyncio.DatagramTransport, self._transport)
        transport.sendto(b, self._addr)

    def datagram_received(self, data: bytes, addr: Addr) -> None:
        """
        Decode the given datagram and put the message to the queue.

        .. note::
            If the queue is full the new message pushes out the oldest one.
            Since the underlying transport with no guarantee of delivery this
            an acceptable way to handle this situation.

        """
        try:
            message = self._decode_message(DarkriftReader(data))
            if len(self._messages) >= self.queue_cap:
                # Since it is UPD message we do not care if we lost something.
                logger.warning(
                    "datagram_received: message queue is full, "
                    "drop the oldest message..."
                )

                self._messages.popleft()

            self._messages.append(message)

            if self._pop_message_waiter is not None:
                self._pop_message_waiter.set_result(None)
                self._pop_message_waiter = None

            return

        except RuntimeError:
            logger.error("failed to decode the message", exc_info=True)

        except Exception:
            logger.error("unexpected excpetion occurred", exc_info=True)

        self.close()

    def error_received(self, exc: Exception) -> None:
        logger.error(f"{self!r} Error received", exc_info=True)
        self.close()

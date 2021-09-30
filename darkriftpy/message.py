from __future__ import annotations

import dataclasses
import enum
from collections.abc import Callable, Iterator
from typing import Any, Optional, Union, cast, get_type_hints

from binio.exceptions import NotEnoughBytes

from darkriftpy import io, types

__all__ = ["Message", "MessageContainer"]

_MESSAGE_FIELDS = "__darkrift_fields"


class _MessageField:
    __slots__ = ("name",)

    def __set_name__(self, owner: type[Message], name: str) -> None:
        self.name = "_" + name

    def __get__(self, instance: Message, owner: Optional[type[Message]] = None) -> Any:
        return getattr(instance, self.name)

    def __set__(self, instance: Message, value: Any) -> None:
        if getattr(instance, self.name, None) != value:
            instance._bytes = None

        setattr(instance, self.name, value)


class _Field:
    __slots__ = ("name", "type", "read", "write")

    def __init__(
        self,
        name: str,
        type_: types.DarkriftType,
        read: Callable[[io.DarkriftReader], types.DarkriftType],
        write: Callable[[io.DarkriftWriter, types.DarkriftType], int],
    ):
        self.name = name
        self.type = type_
        self.read = read
        self.write = write


class MessageType(enum.IntEnum):
    USER = 0x00
    CONNECTION_ID = 0x80

    UNKNOWN = 0xFF


class DarkriftMessage:
    """
    DarkriftMessage is a basic Darkrift2 protocol communication structure.

    The ``tag`` argument specifies the user-level tagging of the message.
    Should be used to recognize the structure of the payload the message contains.

    The ``bytes_`` argument contains the message's encoded payload.

    The ``msg_type`` argument specifies the internal type of the message.
    This information is currently for internal use.

    """

    __slots__ = ("_tag", "_bytes", "_msg_type")

    def __init__(
        self, tag: int, bytes_: bytes, msg_type: MessageType = MessageType.USER
    ) -> None:
        self._tag = tag
        self._bytes: Optional[bytes] = bytes_
        self._msg_type = msg_type

    def get_reader(self) -> io.DarkriftReader:
        return io.DarkriftReader(self.bytes)

    @property
    def type(self) -> MessageType:
        """
        Internal Darkrift type of the message.

        see :class:`~darkriftpy.proto.MessageType`

        """
        return self._msg_type

    @property
    def tag(self) -> int:
        """
        Message tag.

        """
        return self._tag

    @property
    def bytes(self) -> bytes:
        """
        Get the message's payload.

        """
        if self._bytes is None:
            return b""

        return self._bytes


_FieldList = list[_Field]
_reserved_fields = ("tag", "type", "bytes")


class MessageMeta(type):
    """Metaclass for all messages"""

    def __new__(
        metacls: type[MessageMeta],
        name: str,
        bases: tuple[type, ...],
        namespace: dict[str, Any],
        **kwargs: dict[str, Any],
    ) -> MessageMeta:
        return super().__new__(metacls, name, bases, namespace)

    def __init__(
        cls,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, Any],
        tag: Union[enum.IntEnum, int],
        **kwargs: dict[str, Any],
    ) -> None:
        super().__init__(name, bases, attrs)

        cls = dataclasses.dataclass(cls)  # type: ignore

        cls._tag = int(tag)

        fields: _FieldList = list()
        thints = get_type_hints(cls)
        for field in dataclasses.fields(cls):
            field_name = field.name

            if field_name in _reserved_fields:
                raise ValueError(f"{field_name} is reserved Message field!")

            field_type = thints[field_name]

            prop = _MessageField()
            prop.__set_name__(cls, field_name)  # type: ignore
            setattr(cls, field_name, prop)

            fields.append(
                _Field(
                    field_name,
                    field_type,
                    io.READERS[field_type],
                    io.WRITERS[field_type],
                )
            )

        setattr(cls, _MESSAGE_FIELDS, fields)

    @property
    def tag(cls) -> int:
        return cls._tag


class Message(DarkriftMessage, metaclass=MessageMeta, tag=0):
    def __post_init__(self) -> None:
        self._bytes: Optional[bytes] = None

    def __init_subclass__(cls, **kwargs: dict[str, Any]) -> None:
        ...

    def _encode_message(self) -> bytes:
        fields = getattr(type(self), _MESSAGE_FIELDS, None)
        if fields is None:
            raise RuntimeError(
                "failed to encode the message's fields. "
                "Is the given object not a darkrift message?"
            )

        writer = io.DarkriftWriter()
        fields = cast(_FieldList, fields)
        for field in fields:
            field.write(writer, getattr(self, field.name))

        return writer.bytes

    @property
    def type(self) -> MessageType:
        return MessageType.USER

    @property
    def bytes(self) -> bytes:
        if self._bytes is None:
            self._bytes = self._encode_message()

        return self._bytes

    @classmethod
    def read(cls, reader: io.DarkriftReader) -> Message:
        """
        Read the message fields from the given reader.

        :param reader: the reader to read from.

        :raises RuntimeError: when failed to decode the next field
        from the given reader.

        """
        fields = getattr(cls, _MESSAGE_FIELDS, None)

        args = dict()
        fields = cast(_FieldList, fields)
        for field in fields:
            try:
                args[field.name] = field.read(reader)
            except NotEnoughBytes:
                raise RuntimeError(
                    f"failed to read {field.name} field from the " f"given reader"
                )

        return cls(**args)

    def write(self, writer: io.DarkriftWriter) -> int:
        return writer.write(self.bytes)


class MessageContainer:
    """
    Is a container for user-definied messages classes.

    The ``messages`` argument is the initial list of
    :class:`~darkrift.message.Message` claseses.

    :meth:`add` can be used as a dcorator for a user-defined message class::

        messages = darkriftpy.message.MessageContainer()

        @messages.add
        class UserMessage(darkrift.message.Message, tag=2)
            user_id: darkrift.types.uint32

    The class supports subscription to retrieve a previously registered
    message class for the given tag::

        messages = darkriftpy.message.MessageContainer()
        messages.add(UserMessage)

        assert messages[2] == UserMessage

    The class also supports iterator protocol over the message classes:

        messages = darkriftpy.message.MessageContainer()
        messages.add(UserMessage)

        for message in messages:
            print(message.tag)

    """

    def __init__(self, messages: list[type[Message]] = list()) -> None:
        self._messages: dict[int, type[Message]] = {
            cast(MessageMeta, m).tag: m for m in messages
        }

    def __contains__(self, tag: Union[enum.IntEnum, int]) -> bool:
        return int(tag) in self._messages

    def __getitem__(self, tag: Union[enum.IntEnum, int]) -> type[Message]:
        return self._messages[int(tag)]

    def __delitem__(self, tag: Union[enum.IntEnum, int]) -> None:
        del self._messages[int(tag)]

    def __iter__(self) -> Iterator[type[Message]]:
        return iter(self._messages.values())

    def add(self, message: type[Message]) -> type[Message]:
        tag: int = message.tag  # type: ignore
        self._messages[tag] = message

        return message

    def remove(self, message: type[Message]) -> None:
        tag: int = message.tag  # type: ignore
        if tag not in self._messages:
            return

        del self._messages[tag]

    def convert(self, message: DarkriftMessage) -> Message:
        """
        Convert the given darkrift message to a user-defined one.

        :param message: a base darkrift message to be converted.

        :raises RuntimeError: if there is no user-defined message registered
        for the given message, or failed to read(decode) the message's payload

        """
        try:
            msg_cls = self._messages[message.tag]
        except KeyError:
            raise RuntimeError(
                f"container does not have a user-defined message class "
                f"for the given message tag: {message.tag}"
            )

        return msg_cls.read(message.get_reader())

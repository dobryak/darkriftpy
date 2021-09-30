from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any, Optional, Union

import binio

from darkriftpy import types

__all__ = ["DarkriftReader", "DarkriftWriter"]


class DarkriftReader(binio.BinaryReader):
    def __init__(self, b: Union[io.BytesIO, bytes]) -> None:
        super().__init__(b, binio.ByteOrder.BIG)

    def _read_list(
        self, read: Callable[[], types.DarkriftType]
    ) -> list[types.DarkriftType]:
        """
        Read a list of values.

        :param read: the reader method to read a single value

        """
        c = self.read_int32()
        v = []

        for i in range(c):
            v.append(read())

        return v

    def read_int8s(self) -> list[types.int8]:
        return self._read_list(self.read_int8)  # type: ignore

    def read_uint8s(self) -> list[types.uint8]:
        return self._read_list(self.read_uint8)  # type: ignore

    def read_int16s(self) -> list[types.int16]:
        return self._read_list(self.read_int16)  # type: ignore

    def read_uint16s(self) -> list[types.uint16]:
        return self._read_list(self.read_uint16)  # type: ignore

    def read_int32s(self) -> list[types.int32]:
        return self._read_list(self.read_int32)  # type: ignore

    def read_uint32s(self) -> list[types.uint32]:
        return self._read_list(self.read_uint32)  # type: ignore

    def read_int64s(self) -> list[types.int64]:
        return self._read_list(self.read_int64)  # type: ignore

    def read_uint64s(self) -> list[types.uint64]:
        return self._read_list(self.read_uint64)  # type: ignore

    def read_singles(self) -> list[float]:
        return self._read_list(self.read_single)  # type: ignore

    def read_doubles(self) -> list[types.double]:
        return self._read_list(self.read_double)  # type: ignore

    def read_byte(self) -> bytes:
        return self.read(1)

    def read_bytes(self) -> bytes:
        return self.read(self.read_int32())

    def read_string(self) -> str:
        return self.read_str(self.read_int32())

    def read_strings(self) -> list[str]:
        return self._read_list(self.read_string)  # type: ignore

    def read_chars(self) -> str:
        return self.read_string()


class DarkriftWriter(binio.BinaryWriter):
    def __init__(self) -> None:
        super().__init__(binio.ByteOrder.BIG)

    @classmethod
    def from_stream(
        cls,
        stream: io.BytesIO,
        byte_order: Optional[binio.ByteOrder] = binio.ByteOrder.BIG,
    ) -> DarkriftWriter:
        writer = cls()
        writer._stream = stream

        return writer

    def _write_list(self, v: list[Any], write: Callable[[Any], int]) -> int:
        c = len(v)
        self.write_int32(c)
        sz = 0
        for i in range(c):
            sz += write(v[i])

        return sz

    def write_int8s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_int8)

    def write_uint8s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_uint8)

    def write_int16s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_int16)

    def write_uint16s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_uint16)

    def write_int32s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_int32)

    def write_uint32s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_int32)

    def write_int64s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_int64)

    def write_uint64s(self, v: list[int]) -> int:
        return self._write_list(v, self.write_uint64)

    def write_singles(self, v: list[float]) -> int:
        return self._write_list(v, self.write_single)

    def write_doubles(self, v: list[float]) -> int:
        return self._write_list(v, self.write_double)

    def write_byte(self, b: bytes) -> int:
        if len(b) < 1:
            return 0

        return self.write(b[:1])

    def write_bytes(self, b: bytes) -> int:
        b_len = len(b)
        if b_len == 0:
            return 0

        self.write_int32(b_len)
        return self.write(b)

    def write_string(self, s: str) -> int:
        b_len = len(s)
        written = self.write_int32(b_len)
        return written + self.write_str(s)

    def write_strings(self, s: list[str]) -> int:
        return self._write_list(s, self.write_string)

    def write_chars(self, cs: str) -> None:
        self.write_string(cs)


READERS: dict[
    type[types.DarkriftType], Callable[[DarkriftReader], types.DarkriftType]
] = {
    bool: DarkriftReader.read_bool,
    str: DarkriftReader.read_string,
    types.char: DarkriftReader.read_char,
    types.int8: DarkriftReader.read_int8,
    types.uint8: DarkriftReader.read_uint8,
    types.int16: DarkriftReader.read_int16,
    types.uint16: DarkriftReader.read_uint16,
    types.int32: DarkriftReader.read_int32,
    types.uint32: DarkriftReader.read_uint32,
    types.int64: DarkriftReader.read_int64,
    types.uint64: DarkriftReader.read_uint64,
    types.double: DarkriftReader.read_double,
    float: DarkriftReader.read_single,
    bytes: DarkriftReader.read_bytes,
    list[str]: DarkriftReader.read_strings,
    list[types.int8]: DarkriftReader.read_int8s,
    list[types.uint8]: DarkriftReader.read_uint8s,
    list[types.int16]: DarkriftReader.read_int16s,
    list[types.uint16]: DarkriftReader.read_uint16s,
    list[types.int32]: DarkriftReader.read_int32s,
    list[types.uint32]: DarkriftReader.read_uint32s,
    list[types.int64]: DarkriftReader.read_int64s,
    list[types.uint64]: DarkriftReader.read_uint64s,
    list[float]: DarkriftReader.read_singles,
    list[types.double]: DarkriftReader.read_doubles,
}

WRITERS: dict[type[types.DarkriftType], Callable[[DarkriftWriter, Any], int]] = {
    bool: DarkriftWriter.write_bool,
    str: DarkriftWriter.write_string,
    types.char: DarkriftWriter.write_char,
    types.int8: DarkriftWriter.write_int8,
    types.uint8: DarkriftWriter.write_uint8,
    types.int16: DarkriftWriter.write_int16,
    types.uint16: DarkriftWriter.write_uint16,
    types.int32: DarkriftWriter.write_int32,
    types.uint32: DarkriftWriter.write_uint32,
    types.int64: DarkriftWriter.write_int64,
    types.uint64: DarkriftWriter.write_uint64,
    types.double: DarkriftWriter.write_double,
    float: DarkriftWriter.write_single,
    bytes: DarkriftWriter.write_bytes,
    list[str]: DarkriftWriter.write_strings,
    list[types.int8]: DarkriftWriter.write_int8s,
    list[types.uint8]: DarkriftWriter.write_uint8s,
    list[types.int16]: DarkriftWriter.write_int16s,
    list[types.uint16]: DarkriftWriter.write_uint16s,
    list[types.int32]: DarkriftWriter.write_int32s,
    list[types.uint32]: DarkriftWriter.write_uint32s,
    list[types.int64]: DarkriftWriter.write_int64s,
    list[types.uint64]: DarkriftWriter.write_uint64s,
    list[float]: DarkriftWriter.write_singles,
    list[types.double]: DarkriftWriter.write_doubles,
}

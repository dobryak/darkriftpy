from typing import NewType, Union

char = NewType("char", str)
int8 = NewType("int8", int)
uint8 = NewType("uint8", int)
int16 = NewType("int16", int)
uint16 = NewType("uint16", int)
int32 = NewType("int32", int)
uint32 = NewType("uint32", int)
int64 = NewType("int64", int)
uint64 = NewType("uint64", int)
double = NewType("double", float)

DarkriftType = Union[
    str,
    bool,
    char,
    int8,
    uint8,
    int16,
    uint16,
    int32,
    uint32,
    int64,
    uint64,
    double,
    bytes,
    float,
    list[str],
    list[int8],
    list[uint8],
    list[int16],
    list[uint16],
    list[int32],
    list[uint32],
    list[int64],
    list[uint64],
    list[float],
    list[double],
]

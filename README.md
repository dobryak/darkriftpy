# DarkRiftPy
DarkRiftPy is [Darkrift2](https://www.darkriftnetworking.com/) written in
Python 3. The implementation is fully compatible with the original version. So
you can write a client side on Python that connects to a Darkrift2 server
written in C# using the original Darkrift2 library, and vice versa.  

DarkRiftPy is built on top of
[asyncio](https://docs.python.org/3/library/asyncio.html), Python's standard
asynchronus I/O library, and provides a convenient high-level async/await API.  

## Installation

```console
$ python3 -m pip install darkriftpy

```

## Quick usage example

A simple exampls contains two separate scripts `client.py` and `server.py` for
client and server respectively.  

After client is connected to the server the latter waits for a darkrift message
with tag 1, which contains a list of int32 integers in the payload. Once the
message with tag 1 is received, the server starts to randomly select a value
from the given list and sends it back to the client.

`client.py`:  

```python
import asyncio
import random


import darkriftpy


RND_POOL = 20

MIN_INT32 = (2 ** 31) * -1
MAX_INT32 = 2 ** 31 - 1


async def process_message(message: darkriftpy.DarkriftMessage) -> None:
    if message.tag != 2:
        raise ValueError("wrong message received")

    num = message.get_reader().read_int32()
    print(f"the server chose the number: {num}")


async def main() -> None:
    try:
        async with darkriftpy.connect("127.0.0.1", 4296, 4296) as client:
            items = [random.randint(MIN_INT32, MAX_INT32) for _ in range(RND_POOL)]

            writer = darkriftpy.DarkriftWriter()
            writer.write_int32s(items)

            await client.send(darkriftpy.DarkriftMessage(1, writer.bytes))

            async for message in client:
                await process_message(message)

            print("connection has been closed by the server")

    except ConnectionError:
        print("failed to connect to the server")


if __name__ == "__main__":
    asyncio.run(main())
```

`server.py`:  

```python
import asyncio
import random


import darkriftpy


async def handle_client(client: darkriftpy.DarkriftClient) -> None:
    message = await client.recv()

    if message.tag != 1:
        raise RuntimeError("wrong client message received")

        client.close()
        await client.wait_closed()
        return

    reader = message.get_reader()
    items = reader.read_int32s()

    while True:
        writer = darkriftpy.DarkriftWriter()
        writer.write_int32(random.choice(items))

        try:
            await client.send(darkriftpy.DarkriftMessage(2, writer.bytes))
        except darkriftpy.ConnectionClosedError:
            print(f"the client({client.connection_id}) has been disconnected")
            await client.wait_closed()
            return

        await asyncio.sleep(1)


async def main() -> None:
    async with darkriftpy.serve(handle_client, "127.0.0.1", 4296, 4296) as server:
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
```

## User defined messages

`darkriftpy` provides a convinient way to create/send/receive user-defined
messages. There is a `Message` class that can be used as a base class for
user-defined ones. The Darkrift tag of a user-defined message is defined by
passing the keyword `tag` argument in the class definition:  

```python
import darkriftpy

class ChooseMessage(darkriftpy.Message, tag=1):
    ...

```

For now, the `ChooseMessage` message contains no payload. Since the
`ChooseMessage` class is implicitly decorated with the
[@dataclass](https://docs.python.org/3/library/dataclasses.html?highlight=dataclass#dataclasses.dataclass)
decorator, the user can define class variables with [type
annotations](https://docs.python.org/3/glossary.html#term-variable-annotation)
which will be automatically deserialized from or serialized to a binary stream
using `DarkriftReader` and `DarkriftWriter` classes. Only the following native
types can be used as a class variable type: `str`, `bytes`, `bool`, `float`.
Since [Darkrift2](https://www.darkriftnetworking.com/) allows to use types
which are not natively available in python, the `darkriftpy.types` module
provides
[NewType](https://docs.python.org/3/library/typing.html?highlight=newtype#typing.NewType)
extensions to cover all the required Darkrift2 types.  

```python
import darkriftpy
from darkriftpy.types import int32


class ChooseMessage(darkriftpy.Message, tag=1):
    items: list[int32]

```

As you can see we used the `int32` type from the `darkriftpy.types` module to
define 4 byte signed integer. Since the `ChooseMessage` class is implicitly
decorated with the
[@dataclass](https://docs.python.org/3/library/dataclasses.html?highlight=dataclass#dataclasses.dataclass)
decorator and there is no custom constructor, the following constructor will be
created automatically:  `__init__(self, items: lsit[int32])`  

Therefore, the `ChooseMessage` class can be instantiated as follows:  

```python
import random


import darkriftpy
from darkriftpy.types import int32


MIN_INT32 = (2 ** 31) * -1
MAX_INT32 = 2 ** 31 - 1


class ChooseMessage(darkriftpy.Message, tag=1):
    items: list[int32]


message = ChooseMessage([random.randint(MIN_INT32, MAX_INT32) for _ in range(10)])

# message.items contains a list with 10 int32 integers

```

Since the `darkriftpy.Message` is inherited from `darkriftpy.DarkriftMessage`
the user-defined message can be passed as is to the `send` method of the
`darkriftpy.DarkriftClient` object.  

To convert a received `darkriftpy.DarkriftMessage` message to the user-defined
one, the user can do the following:  

```python
...

client: darkriftpy.DarkriftClient
message: darkriftpy.DarkriftMessage = await client.recv()

try:
    choose_message = ChooseMessage.read(message.get_reader())
except RuntimeError:
    # failed to parse the received message
    ...

print(choose_message.items)

```

The `darkriftpy` package provides the `MessageContainer` class to
simplify the message serialization and de-siarilization.  

```python
import darkriftpy
from darkriftpy.types import int32


messages = darkriftpy.MessageContainer()


@messages.add
class ChooseMessage(darkriftpy.Message, tag=1):
    items: list[int32]


@messages.add
class ChoiceMessage(darkriftpy.Message, tag=2):
    item: int32

...

client: darkriftpy.DarkriftClient
message: darkriftpy.DarkriftMessage = await client.recv()

try:
    msg = messages.convert(message)
except RuntimeError:
    # failed to convert the received darkrift message
    # to the user-defined one

if isinstance(msg, ChooseMessage):
    print(msg.items)
elif isinstance(msg, ChoiceMessage):
    print(msg.item)

```

We used the `add` method of the `MessageContainer` class as decorator to add
the user-defined class into the message container `messages`.  
The `convert` method of the `MessageContainer` class allows us to convert a raw
darkrift message to the user-defined specific one.  

Using all these we can create a client wrapper that will return already
deserialized messages.  

```python
from collections.abc import AsyncIterator


import darkriftpy


class Client:
    def __init__(
        self, client: darkriftpy.DarkriftClient, messages: darkriftpy.MessageContainer
    ):
        self._client = client
        self._messages = messages

    async def recv(self) -> darkriftpy.DarkriftMessage:
        message = await self._client.recv()

        try:
            return self._messages.convert(message)
        except RuntimeError:
            # just return the message as is
            pass

        return message

    async def send(self, message: darkriftpy.DarkriftMessage, reliable: bool = True) -> None:
        await self._client.send(message, reliable)

    def __aiter__(self) -> AsyncIterator[darkriftpy.DarkriftMessage]:
        return self

    async def __anext__(self) -> darkriftpy.DarkriftMessage:
        """
        Returns the next message.

        Stop iteration when the connection is closed.

        """
        try:
            return await self.recv()
        except darkrift.ConnectionClosedError:
            raise StopAsyncIteration()

```

So now we can use the client wrapper to send and receive user specified
messages.

Let's update the first example to use all described features.

`client.py`:  

```python
import asyncio
import random
from collections.abc import AsyncIterator

import darkriftpy
from darkriftpy.types import int32


RND_POOL = 20

MIN_INT32 = (2 ** 31) * -1
MAX_INT32 = 2 ** 31 - 1


messages = darkriftpy.MessageContainer()


@messages.add
class ChooseMessage(darkriftpy.Message, tag=1):
    items: list[int32]


@messages.add
class ChoiceMessage(darkriftpy.Message, tag=2):
    item: int32


class Client:
    def __init__(
        self, client: darkriftpy.DarkriftClient, messages: darkriftpy.MessageContainer
    ):
        self._client = client
        self._messages = messages

    async def recv(self) -> darkriftpy.DarkriftMessage:
        message = await self._client.recv()

        try:
            return self._messages.convert(message)
        except RuntimeError:
            # just return the message as is
            pass

        return message

    async def send(
        self, message: darkriftpy.DarkriftMessage, reliable: bool = True
    ) -> None:
        await self._client.send(message, reliable)

    def __aiter__(self) -> AsyncIterator[darkriftpy.DarkriftMessage]:
        return self

    async def __anext__(self) -> darkriftpy.DarkriftMessage:
        """
        Returns the next message.

        Stop iteration when the connection is closed.

        """
        try:
            return await self.recv()
        except darkrift.ConnectionClosedError:
            raise StopAsyncIteration()


async def process_message(message: darkriftpy.DarkriftMessage) -> None:
    if not isinstance(message, ChoiceMessage):
        raise ValueError("wrong message received")

    print(f"the server chose the number: {message.item}")


async def main():
    try:
        c: darkriftpy.DarkriftClient
        async with darkriftpy.connect("127.0.0.1", 4296, 4296) as c:
            client = Client(c, messages)
            choose_message = ChooseMessage(
                [random.randint(MIN_INT32, MAX_INT32) for _ in range(RND_POOL)]
            )

            await client.send(choose_message)

            async for message in client:
                await process_message(message)

            print("Connection has been closed by the server")

    except ConnectionError:
        print("failed to connect to the server")


if __name__ == "__main__":
    asyncio.run(main())

```

`server.py`:  

```python
import asyncio
import random
from collections.abc import AsyncIterator

import darkriftpy
from darkriftpy.types import int32


messages = darkriftpy.MessageContainer()


@messages.add
class ChooseMessage(darkriftpy.Message, tag=1):
    items: list[int32]


@messages.add
class ChoiceMessage(darkriftpy.Message, tag=2):
    item: int32


class Client:
    def __init__(
        self, client: darkriftpy.DarkriftClient, messages: darkriftpy.MessageContainer
    ):
        self._client = client
        self._messages = messages

    async def recv(self) -> darkriftpy.DarkriftMessage:
        message = await self._client.recv()

        try:
            return self._messages.convert(message)
        except RuntimeError:
            # just return the message as is
            pass

        return message

    async def send(
        self, message: darkriftpy.DarkriftMessage, reliable: bool = True
    ) -> None:
        await self._client.send(message, reliable)

    def __aiter__(self) -> AsyncIterator[darkriftpy.DarkriftMessage]:
        return self

    async def __anext__(self) -> darkriftpy.DarkriftMessage:
        """
        Returns the next message.

        Stop iteration when the connection is closed.

        """
        try:
            return await self.recv()
        except darkrift.ConnectionClosedError:
            raise StopAsyncIteration()


async def handle_client(c: darkriftpy.DarkriftClient) -> None:
    client = Client(c, messages)

    message = await client.recv()
    if not isinstance(message, ChooseMessage):
        raise RuntimeError("wrong client message received")

        c.close()
        await c.wait_closed()
        return

    while True:
        choice_message = ChoiceMessage(random.choice(message.items))

        try:
            await client.send(choice_message)
        except darkriftpy.ConnectionClosedError:
            print(f"the client({c.connection_id}) has been disconnected")
            await c.wait_closed()
            return

        await asyncio.sleep(1)


async def main():
    async with darkriftpy.serve(handle_client, "127.0.0.1", 4296, 4296) as server:
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())

```


## TODO

 [  ] - Add multiprocessing support to improve performance and scalability
 (Fork + Multiplexing I/O).  
 [  ] - Cover the codebase with tests ;).  

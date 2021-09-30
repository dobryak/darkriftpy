"""
:mod:`darkriftpy.exceptions` defines the following exception hierarchy:

* :exc:`DarkriftException`
    * :exc:`ProtocolError`
    * :exc:`HandshakeError`
    * :exc:`ConnectionTimeoutError`
    * :exc:`ConnectionClosedError`

"""

__all__ = [
    "DarkriftException",
    "ProtocolError",
    "HandshakeError",
    "ConnectionTimeoutError",
    "ConnectionClosedError",
]


class DarkriftException(Exception):
    """
    Base class for all exceptions

    """


class ProtocolError(DarkriftException):
    """
    Raised when protocol is broken

    """


class HandshakeError(DarkriftException):
    """
    Raised when handshake failed for some reasons

    """


class ConnectionTimeoutError(DarkriftException):
    """
    Raised when failed to connect to a TCP endpoint of a Darkrift server

    """


class ConnectionClosedError(DarkriftException):
    """
    Raised when the connection is closed for any reason

    """

"""Network deny-by-default guard loaded only by run_isolated_tests.sh."""
import socket


def _network_disabled(*_args, **_kwargs):
    raise OSError("Network access is disabled by the isolated test harness")


# Do not affect AF_UNIX local IPC; deny all IPv4/IPv6 connect attempts.
_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_getaddrinfo = socket.getaddrinfo


def _guard_connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        return _network_disabled(address)
    return _original_connect(self, address)


def _guard_connect_ex(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        return _network_disabled(address)
    return _original_connect_ex(self, address)


socket.socket.connect = _guard_connect
socket.socket.connect_ex = _guard_connect_ex
socket.getaddrinfo = _network_disabled
socket.create_connection = _network_disabled

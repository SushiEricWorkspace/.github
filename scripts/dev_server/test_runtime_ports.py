"""wildcard/dual-stack/TCP/UDP境界、予約rollback、実占有を反証する。"""
import socket
import sqlite3
import unittest

from .registry import RegistryError, SCHEMA
from .runtime_ports import endpoint, overlap, prebind, reserve, require_listeners, network_endpoints


def ep(address="127.0.0.1", port=25790, protocol="tcp", v6=False):
    return endpoint({"host": "localhost", "protocol": protocol, "address": address, "port": port, "v6Only": v6})


class PortTests(unittest.TestCase):
    def test_overlap_ipv4_ipv6_wildcard_and_protocol(self):
        self.assertTrue(overlap(ep(), ep("0.0.0.0")))
        self.assertTrue(overlap(ep(), ep("::")))
        self.assertFalse(overlap(ep(), ep("::", v6=True)))
        self.assertFalse(overlap(ep(), ep("::1")))
        self.assertTrue(overlap(ep("::1"), ep("::", v6=True)))
        self.assertFalse(overlap(ep(), ep(protocol="udp")))
        self.assertFalse(overlap(ep(), ep(port=25791)))
        self.assertFalse(overlap(ep(), ep("127.0.0.2")))

    def test_network_range_and_unknown_endpoint_are_rejected(self):
        self.assertEqual(25790, network_endpoints([25700, 25799], {"tcp": ep()})["tcp"]["port"])
        for value in (ep(port=25665), ep(port=25800)):
            with self.assertRaises(RegistryError):
                network_endpoints([25700, 25799], {"tcp": value})
        for value in (dict(ep(), address="localhost"), dict(ep(), address="::ffff:127.0.0.1"),
                      dict(ep(), protocol="sctp"), dict(ep(), v6Only=True)):
            with self.assertRaises(RegistryError):
                endpoint(value)

    def test_atomic_reservation_conflict_and_protocol_separation(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.executescript(SCHEMA)
        with connection:
            reserve(connection, "one", "run-one", {"tcp": ep()})
        with self.assertRaises(RegistryError), connection:
            reserve(connection, "two", "run-two", {"udp": ep(protocol="udp"), "conflict": ep("0.0.0.0")})
        self.assertEqual(1, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])
        with connection:
            reserve(connection, "two", "run-two", {"udp": ep(protocol="udp")})
        self.assertEqual(2, connection.execute("SELECT count(*) FROM endpoint_reservations").fetchone()[0])

    def test_prebind_preserves_unrelated_live_socket(self):
        with socket.socket() as external:
            external.bind(("127.0.0.1", 0))
            external.listen()
            value = ep(port=external.getsockname()[1])
            with self.assertRaises(OSError), prebind({"minecraft": value}):
                pass
            with socket.create_connection(external.getsockname(), timeout=1):
                accepted, _ = external.accept()
                accepted.close()
        with prebind({"minecraft": value}):
            pass

    def test_listener_id_and_pid_socket_must_both_match(self):
        expected = {"minecraft": ep(), "management": ep(port=25791)}
        proof = [dict(service=service, **{key: value[key] for key in ("protocol", "address", "port")})
                 for service, value in expected.items()]
        actual = list(expected.values())
        require_listeners(expected, proof, actual)
        for reported, owned in ((proof, actual[:1]), (proof, actual + [ep(port=25792)]),
                                 ([dict(proof[0], port=25791), dict(proof[1], port=25790)], actual),
                                 (proof[:1] * 2, actual), ([dict(proof[0], address="0.0.0.0"), proof[1]], actual)):
            with self.assertRaises(RegistryError):
                require_listeners(expected, reported, owned)

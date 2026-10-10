"""host/protocol/address/portの実予約と事前bind。OS確認をDBの短いtransactionへ持ち込まない。"""
from contextlib import contextmanager
import ipaddress
import os
import socket

from .registry import require, fields


def endpoint(value: dict) -> dict:
    fields(value, {"host", "protocol", "address", "port", "v6Only"})
    require(value["host"] == "localhost" and value["protocol"] in ("tcp", "udp"),
            "INVALID_ENDPOINT", "ローカルのTCP/UDPだけを使用します")
    require(type(value["port"]) is int and 1024 <= value["port"] <= 65535 and type(value["v6Only"]) is bool,
            "INVALID_ENDPOINT", "portまたはIPv6方針が不正です")
    try:
        address = ipaddress.ip_address(value["address"])
    except ValueError:
        require(False, "INVALID_ENDPOINT", "具体的なIPアドレスを指定してください")
    require("%" not in str(address) and not getattr(address, "ipv4_mapped", None),
            "INVALID_ENDPOINT", "scopeやIPv4-mappedの共有は対応していません")
    require(address.version == 6 or not value["v6Only"], "INVALID_ENDPOINT", "IPv4へv6Onlyを指定できません")
    return dict(value, address=str(address))


def overlap(one: dict, two: dict) -> bool:
    """不明なdual-stack共有を許可せず、wildcardは同family全体へ重なる。"""
    if (one["host"], one["protocol"], one["port"]) != (two["host"], two["protocol"], two["port"]):
        return False
    a, b = ipaddress.ip_address(one["address"]), ipaddress.ip_address(two["address"])
    if a.version == b.version:
        return a == b or a.is_unspecified or b.is_unspecified
    v6, policy = (a, one) if a.version == 6 else (b, two)
    return v6.is_unspecified and not policy["v6Only"]


def instance_endpoints(declaration: dict) -> dict[str, dict]:
    address = declaration["minecraftBind"]
    require(address != "lan-explicit", "UNSUPPORTED_ENDPOINT", "LAN用具体アドレスの運用契約は未接続です")
    return {service: endpoint({"host": declaration["host"], "protocol": "tcp",
                              "address": declaration["managementBind"] if service == "management" else address,
                              "port": port, "v6Only": False})
            for service, port in declaration["ports"].items()}


def reserve(connection, instance: str, run_id: str, endpoints: dict[str, dict]) -> None:
    """DB transaction内で全endpointを予約する。通信試験範囲と固定宣言は呼出側で分ける。"""
    previous = [dict(row) for row in connection.execute("SELECT * FROM endpoint_reservations")]
    planned = []
    for service, value in endpoints.items():
        value = endpoint(value)
        require(not any(overlap(value, other) for other in previous + planned),
                "PORT_RESERVED", "待受予約が競合しています")
        planned.append(value)
        connection.execute("INSERT INTO endpoint_reservations VALUES (?,?,?,?,?,?,?,?)",
                           (run_id, instance, service, value["host"], value["protocol"], value["address"], value["port"], int(value["v6Only"])))


def network_endpoints(port_range: list[int], requested: dict[str, dict]) -> dict[str, dict]:
    """通信試験はinventory内の明示portだけを使い、空き候補へ自動変更しない。"""
    lower, upper = port_range
    result = {service: endpoint(value) for service, value in requested.items()}
    require(all(lower <= value["port"] <= upper for value in result.values()),
            "INVALID_ENDPOINT", "通信試験portがinventory範囲外です")
    return result


@contextmanager
def prebind(endpoints: dict[str, dict]):
    """全socketを同時に保持して空きを確認する。listen競合相手へ接続・停止しない。"""
    probes = []
    try:
        for value in endpoints.values():
            value = endpoint(value)
            family = socket.AF_INET6 if ipaddress.ip_address(value["address"]).version == 6 else socket.AF_INET
            probe = socket.socket(family, socket.SOCK_STREAM if value["protocol"] == "tcp" else socket.SOCK_DGRAM)
            probes.append(probe)
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            if family == socket.AF_INET6:
                probe.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, int(value["v6Only"]))
            probe.bind((value["address"], value["port"]))
            if value["protocol"] == "tcp":
                probe.listen(1)
        yield
    finally:
        for probe in probes:
            probe.close()


def socket_keys(values: list[dict]) -> set[tuple]:
    return {(item["protocol"], str(ipaddress.ip_address(item["address"])), item["port"]) for item in values}


def require_listeners(expected: dict[str, dict], proof: list[dict], actual: list[dict]) -> None:
    """Modの報告と、識別済みPIDが実際に所有するOS socketを両方要求する。"""
    require(type(proof) is list and len(proof) == len(expected), "LISTENER_MISMATCH", "Modの待受集合が一致しません")
    for item in proof:
        require(type(item) is dict and item.keys() == {"service", "protocol", "address", "port"},
                "LISTENER_MISMATCH", "Modの待受記録が不正です")
        require(item["service"] in expected, "LISTENER_MISMATCH", "未知の待受があります")
        require(socket_keys([item]) == socket_keys([expected[item["service"]]]),
                "LISTENER_MISMATCH", "serviceと待受が一致しません")
    require(len({item["service"] for item in proof}) == len(expected)
            and socket_keys(actual) == socket_keys(list(expected.values())),
            "LISTENER_MISMATCH", "実待受またはsocket所有が一致しません")

"""Validate explicit public seed data; never host account credentials."""

import base64
import json
import ipaddress
import re


def network(value):
    if (not isinstance(value, dict) or set(value) != {'address', 'gateway', 'dns'}
            or any(not isinstance(part, str) for part in value.values())):
        raise ValueError('Expected fixed guest address, gateway and DNS')
    address = ipaddress.IPv4Interface(value['address'])
    gateway = ipaddress.IPv4Address(value['gateway'])
    dns = ipaddress.IPv4Address(value['dns'])
    if (address.network.prefixlen != 24 or gateway != address.network.network_address + 1
            or dns != gateway or address.ip in (gateway, address.network.network_address,
                                                address.network.broadcast_address)):
        raise ValueError('Guest network differs from the fixed /24 contract')
    return {'address': str(address), 'gateway': str(gateway), 'dns': str(dns)}


def validate(seed):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", seed["name"]):
        raise ValueError("Invalid machine name")
    if not re.fullmatch(r"[0-9a-f]{32}", seed["id"]):
        raise ValueError("Invalid machine identity")
    words = seed["ssh_key"].split()
    if len(words) < 2 or words[0] != "ssh-ed25519":
        raise ValueError("Expected an Ed25519 management public key")
    blob = base64.b64decode(words[1], validate=True)
    if len(blob) != 51 or blob[:19] != b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20":
        raise ValueError("Invalid Ed25519 management public key")
    result = {"name": seed["name"], "id": seed["id"], "ssh_key": " ".join(words[:2])}
    if 'network' in seed:
        result['network'] = network(seed['network'])
    return result


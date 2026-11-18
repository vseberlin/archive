"""The existing neutral ISS check convention; no allocator or identity registry."""
import hashlib
import re

ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'
PATTERN = re.compile(r'^ISS-([0-9]{4})-([' + ALPHABET + r']{4})-([' + ALPHABET + r'])$')


def valid_uid(value):
    if not isinstance(value, str):
        return False
    match = PATTERN.fullmatch(value)
    return bool(match and match[3] == ALPHABET[
        hashlib.sha256((match[1] + match[2]).encode('ascii')).digest()[0] & 31])

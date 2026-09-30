"""WBI permutation and canonical query encoding; public protocol sources are in the notes."""

import hashlib
import re
from urllib.parse import quote, urlencode, urlsplit

from .errors import ProtocolFault

# Protocol permutation, not an account secret.
PERMUTATION = (
    46,
    47,
    18,
    2,
    53,
    8,
    23,
    32,
    15,
    50,
    10,
    31,
    58,
    3,
    45,
    35,
    27,
    43,
    5,
    49,
    33,
    9,
    42,
    19,
    29,
    28,
    14,
    39,
    12,
    38,
    41,
    13,
    37,
    48,
    7,
    16,
    24,
    55,
    40,
    61,
    26,
    17,
    0,
    1,
    60,
    51,
    30,
    4,
    22,
    25,
    54,
    21,
    56,
    59,
    6,
    63,
    57,
    62,
    11,
    36,
    20,
    34,
    44,
    52,
)


def mixin_key(img_key: str, sub_key: str) -> str:
    if not re.fullmatch(r"[a-fA-F0-9]{64}", img_key + sub_key):
        raise ProtocolFault()
    combined = img_key + sub_key
    return "".join(combined[index] for index in PERMUTATION)[:32]


def key_from_nav(data: dict) -> str:
    try:
        images = data["wbi_img"]
        keys = [
            urlsplit(images[name]).path.rsplit("/", 1)[-1].split(".")[0]
            for name in ("img_url", "sub_url")
        ]
        return mixin_key(*keys)
    except (KeyError, TypeError, ValueError):
        raise ProtocolFault() from None


def sign(params: dict, key: str, timestamp: int) -> dict[str, str]:
    clean = {
        name: "".join(char for char in str(value) if char not in "!'()*")
        for name, value in params.items()
        if name not in {"w_rid", "wts"}
    }
    clean["wts"] = str(timestamp)
    query = urlencode(sorted(clean.items()), quote_via=quote, safe="")
    clean["w_rid"] = hashlib.md5((query + key).encode(), usedforsecurity=False).hexdigest()
    return clean

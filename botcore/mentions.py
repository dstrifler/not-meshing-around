"""Resolve human node references to the Meshtastic @!xxxxxxxx wire format."""

import re
import unicodedata
from dataclasses import dataclass


HEX_RUN = re.compile(r"![0-9a-fA-F]+")
HEX_NUMBER = re.compile(r"0[xX]([0-9a-fA-F]+)")
WORD = re.compile(r"[0-9A-Za-z]+")
# Keep even malformed longer hex runs together: splitting one after eight
# digits could accidentally turn its first packet into a valid mention.
ATOMIC_MENTION = re.compile(r"@![0-9a-fA-F]+")


@dataclass(frozen=True)
class NodeIdentity:
    number: int
    long_name: str = ""
    short_name: str = ""


def valid_number(number):
    return type(number) is int and 0 <= number <= 0xffffffff


def word_character(char):
    return char.isalnum() or char == "_" or unicodedata.category(char).startswith("M")


def boundary(text, end):
    if end == len(text):
        return True
    if word_character(text[end]):
        return False
    # Don't convert a prefix of an unknown dotted/dashed name, address, or MAC.
    return not (text[end] in "./\\:-" and end + 1 < len(text) and word_character(text[end + 1]))


class MentionResolver:
    def __init__(self, nodes=()):
        self.names = {}
        self.numbers = set()
        for node in nodes:
            if not isinstance(node, NodeIdentity) or not valid_number(node.number):
                continue
            self.numbers.add(node.number)
            for name in (node.long_name, node.short_name):
                if isinstance(name, str) and name.strip() and not any(char in name for char in "\r\n\t"):
                    self.names.setdefault(name.strip().casefold(), set()).add(node.number)
        self.max_name = max(map(len, self.names), default=0)

    def reference(self, text):
        """Return consumed characters and one unambiguous numeric identity."""
        if text.startswith("!"):
            match = HEX_RUN.match(text)
            if match and len(match[0]) == 9:
                number = int(match[0][1:], 16)
                return len(match[0]), number if number in self.numbers else None
            return 0, None
        if text.lower().startswith("0x"):
            match = HEX_NUMBER.match(text)
            if match and len(match[1]) <= 8 and boundary(text, match.end()):
                number = int(match[1], 16)
                return match.end(), number if number in self.numbers else None
            return 0, None

        length, targets = 0, set()
        for end in range(1, min(len(text), self.max_name) + 1):
            if boundary(text, end):
                named = self.names.get(text[:end].casefold())
                if named:
                    length, targets = end, set(named)

        match = WORD.match(text)
        if match and boundary(text, match.end()):
            word = match[0]
            number = None
            if word.isascii() and word.isdigit() and len(word) <= 10:
                number = int(word, 10)
            elif len(word) == 8 and re.fullmatch(r"[0-9a-fA-F]{8}", word):
                number = int(word, 16)
            if number in self.numbers:
                if match.end() > length:
                    length, targets = match.end(), {number}
                elif match.end() == length:
                    targets.add(number)
        return length, next(iter(targets)) if len(targets) == 1 else None

    def normalize(self, text):
        result, cursor = [], 0
        for match in re.finditer("@", text):
            start = match.start()
            if start < cursor:
                continue
            # Explicit wire tokens can appear anywhere, as in the apps. Human
            # names start a word; avoid rewriting email addresses and URL paths.
            if (not text.startswith("@!", start) and start and
                    (word_character(text[start - 1]) or text[start - 1] in "./\\@+-")):
                continue
            length, number = self.reference(text[start + 1:])
            if not length:
                continue
            end = start + 1 + length
            result.append(text[cursor:start])
            result.append(f"@!{number:08x}" if number is not None else text[start:end])
            cursor = end
        result.append(text[cursor:])
        return "".join(result)


def payload_units(text):
    """Yield characters and indivisible node-ID tokens for byte splitting."""
    cursor = 0
    for match in ATOMIC_MENTION.finditer(text):
        yield from text[cursor:match.start()]
        yield match[0]
        cursor = match.end()
    yield from text[cursor:]

"""The ``<cupps>`` message envelope (TS 01.04.0004 section 26.12).

Every interface message is a UTF-8 XML document whose root is ``<cupps>``,
carrying a ``messageID`` used to pair a response with its request and a
``messageName`` naming the single child element that follows.

Section 26.12 is emphatic that *element* order is significant while attribute
order is not, so the builder here preserves the order in which children are
appended and never reorders them.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

#: Default namespace for the main interface schema at interface level 01.04.
CUPPS_NS_01_04 = "http://www.cupps.aero/cupps/01.04"
CUPPS_NS_01_03 = "http://www.cupps.aero/cupps/01.03"
CUPPS_NS_01_01 = "http://www.cupps.aero/cupps/01.01"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

#: Namespace by interface level. The handshake schema (section 26.6 note 4) is
#: deliberately separate and carries no default namespace, so handshake
#: messages are built with ``namespace=None``.
NAMESPACE_BY_LEVEL = {
    "01.01": CUPPS_NS_01_01,
    "01.03": CUPPS_NS_01_03,
    "01.04": CUPPS_NS_01_04,
}

#: Messages belonging to the handshake schema rather than the main schema.
HANDSHAKE_MESSAGES = frozenset(
    {
        "interfaceLevelsAvailableRequest",
        "interfaceLevelsAvailableResponse",
        "interfaceLevelRequest",
        "interfaceLevelResponse",
    }
)

_DOCTYPE_RE = re.compile(rb"<!DOCTYPE", re.IGNORECASE)
_NS_STRIP_RE = re.compile(r"^\{[^}]*\}")


class MessageParseError(Exception):
    """The body could not be parsed into a valid ``<cupps>`` message.

    The receiver answers this with ``<sessionErrorEvent>`` carrying
    eventType ``bodyParseFailure`` and the offending body in Base64
    (section 26.6), then closes the socket.
    """


def _localname(tag: str) -> str:
    """Strip any ``{namespace}`` prefix from an ElementTree tag."""
    return _NS_STRIP_RE.sub("", tag)


@dataclass
class Element:
    """A message element: a name, ordered attributes, children and text."""

    name: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Element"] = field(default_factory=list)
    text: Optional[str] = None

    def add(self, child: "Element") -> "Element":
        """Append ``child``, preserving document order, and return it."""
        self.children.append(child)
        return child

    def find(self, name: str) -> Optional["Element"]:
        """First direct child named ``name``, or ``None``."""
        for child in self.children:
            if child.name == name:
                return child
        return None

    def findall(self, name: str) -> list["Element"]:
        """Every direct child named ``name``, in document order."""
        return [child for child in self.children if child.name == name]

    def iter(self, name: Optional[str] = None) -> Iterator["Element"]:
        """Depth-first walk over this element and its descendants."""
        if name is None or self.name == name:
            yield self
        for child in self.children:
            yield from child.iter(name)

    def get(self, attr: str, default: Optional[str] = None) -> Optional[str]:
        return self.attrs.get(attr, default)

    def get_bool(self, attr: str, default: bool = False) -> bool:
        """Read an ``xs:boolean`` attribute, accepting ``1``/``0``."""
        raw = self.attrs.get(attr)
        if raw is None:
            return default
        return raw.strip().lower() in ("true", "1")

    def get_int(self, attr: str, default: Optional[int] = None) -> Optional[int]:
        raw = self.attrs.get(attr)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            return default


@dataclass
class Message:
    """A complete ``<cupps>`` envelope."""

    message_name: str
    message_id: int
    body: Element
    namespace: Optional[str] = CUPPS_NS_01_04

    @property
    def result(self) -> Optional[str]:
        """The ``result`` attribute of the body, present on every response."""
        return self.body.get("result")

    @property
    def is_ok(self) -> bool:
        """True when the body reports a success result.

        ``OK-deviceAlreadyLocked`` (Listing 30.22 discussion) is a success, so
        any result beginning ``OK`` counts.
        """
        result = self.result
        return result is not None and result.startswith("OK")

    def find(self, name: str) -> Optional[Element]:
        return self.body.find(name)

    def encode(self) -> bytes:
        """Serialise to the UTF-8 bytes that go on the wire."""
        return _serialise(self).encode("utf-8")


def _serialise(message: Message) -> str:
    root_attrs: dict[str, str] = {}
    if message.namespace:
        root_attrs["xmlns"] = message.namespace
    root_attrs["xmlns:xsi"] = XSI_NS
    root_attrs["messageID"] = str(message.message_id)
    root_attrs["messageName"] = message.message_name

    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<cupps"]
    for key, value in root_attrs.items():
        parts.append(f' {key}="{_escape_attr(value)}"')
    parts.append(">")
    parts.append(_serialise_element(message.body))
    parts.append("</cupps>")
    return "".join(parts)


def _serialise_element(element: Element) -> str:
    parts = [f"<{element.name}"]
    for key, value in element.attrs.items():
        parts.append(f' {key}="{_escape_attr(value)}"')
    if not element.children and element.text is None:
        parts.append("/>")
        return "".join(parts)
    parts.append(">")
    if element.text is not None:
        parts.append(_escape_text(element.text))
    for child in element.children:
        parts.append(_serialise_element(child))
    parts.append(f"</{element.name}>")
    return "".join(parts)


def _escape_attr(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build(
    message_name: str,
    message_id: int,
    *,
    body: Optional[Element] = None,
    attrs: Optional[dict[str, Any]] = None,
    interface_level: str = "01.04",
) -> Message:
    """Build a message whose body element is named after ``message_name``.

    Handshake messages are emitted without a default namespace, matching
    Listings 28.1-28.4; every other message carries the namespace for the
    chosen interface level.
    """
    if body is None:
        body = Element(message_name)
    if attrs:
        for key, value in attrs.items():
            if value is None:
                continue
            body.attrs[key] = _to_xml_value(value)
    namespace = (
        None
        if message_name in HANDSHAKE_MESSAGES
        else NAMESPACE_BY_LEVEL.get(interface_level, CUPPS_NS_01_04)
    )
    return Message(
        message_name=message_name,
        message_id=message_id,
        body=body,
        namespace=namespace,
    )


def _to_xml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def parse(raw: bytes) -> Message:
    """Parse wire bytes into a :class:`Message`.

    Raises :class:`MessageParseError` for anything that is not a well-formed
    ``<cupps>`` envelope, which the caller reports as a ``bodyParseFailure``
    sessionErrorEvent.
    """
    # A DOCTYPE is never legal in a CUPPS message and is the vector for entity
    # expansion attacks, so refuse it before handing bytes to the parser.
    if _DOCTYPE_RE.search(raw):
        raise MessageParseError("DOCTYPE declarations are not permitted")

    try:
        root = ET.fromstring(raw.decode("utf-8"))
    except (ET.ParseError, UnicodeDecodeError) as exc:
        raise MessageParseError(f"XML parse failure: {exc}") from exc

    if _localname(root.tag) != "cupps":
        raise MessageParseError(
            f"root element is {_localname(root.tag)!r}, expected 'cupps'"
        )

    message_name = root.get("messageName")
    if not message_name:
        raise MessageParseError("root element is missing messageName")

    raw_id = root.get("messageID")
    if raw_id is None:
        raise MessageParseError("root element is missing messageID")
    try:
        message_id = int(raw_id)
    except ValueError as exc:
        raise MessageParseError(f"messageID {raw_id!r} is not an integer") from exc

    children = list(root)
    if len(children) != 1:
        raise MessageParseError(
            f"<cupps> must carry exactly one body element, found {len(children)}"
        )

    body = _convert(children[0])
    if body.name != message_name:
        raise MessageParseError(
            f"messageName {message_name!r} does not match body element "
            f"{body.name!r}"
        )

    namespace_match = re.match(r"^\{([^}]*)\}", root.tag)
    namespace = namespace_match.group(1) if namespace_match else None
    return Message(
        message_name=message_name,
        message_id=message_id,
        body=body,
        namespace=namespace,
    )


def _convert(node: ET.Element) -> Element:
    text = node.text.strip() if node.text and node.text.strip() else None
    element = Element(
        name=_localname(node.tag),
        attrs={_localname(k): v for k, v in node.attrib.items()},
        text=text,
    )
    for child in node:
        element.children.append(_convert(child))
    return element

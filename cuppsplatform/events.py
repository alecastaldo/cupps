"""Platform events and event subscriptions (TS 01.04.0004 chapter 31).

Chapter 31 makes Event Subscription **Defined, Required**, and section 10.3
reinforces it: a platform must send device state change events to applications
that asked for them via ``<subscriptionAddRequest>``.

The subscription model is token-based rather than connection-based.  An
application presents an ``eventToken`` when it authenticates (section 29.3);
events it raises are tagged with that token.  A second application that wants
to observe them subscribes with the *same* token value.  That is what lets a
management application watch a handling application it did not start.

Section 31.1 is explicit that a platform must accept a subscription to a token
it has never seen, because otherwise a management application could only ever
start *after* the application it monitors -- which "undermines the concept of
the management application".  So an unknown token is a valid subscription that
simply yields nothing yet.

One asymmetry worth knowing: the platform machine has no ``pStpEnteredEvent``
or ``pStpExitedEvent``.  Every other machine has its stopped-state events,
because a *running* platform reports them about its objects; a stopped
platform cannot report anything about itself.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .states import MACHINES, StateChange

log = logging.getLogger("cuppsplatform.events")

#: Events that are not state entries or exits, taken from chapter 31 and the
#: sections that raise them.
NON_STATE_EVENTS = frozenset(
    {
        "applicationInstBlockEvent",
        "cotsApplicationInterfaceEvent",
        "dataAvailableNoLockerEvent",
        "deviceCommsNotificationEvent",
        "deviceLockExpiredEvent",
        "devicePollingTooFastEvent",
        "exceptionEvent",
        "illogicalMessageErrorEvent",
        "interfaceLevelErrorEvent",
        "logErrorEvent",
        "messageAccumulationTimeErrorEvent",
        "messageIDErrorEvent",
        "messageResponseTimeErrorEvent",
        "printRequestStatusEvent",
        "sessionErrorEvent",
        "simpleHostPrintStatusEvent",
    }
)

#: The platform reports no events about its own stopped state: there is
#: nothing running to report them. Every other machine does have them.
_SUPPRESSED_STATE_EVENTS = frozenset({"pStpEnteredEvent", "pStpExitedEvent"})


def state_event_names() -> frozenset[str]:
    """Every ``<xxxEnteredEvent>`` / ``<xxxExitedEvent>`` a platform raises."""
    names = set()
    for definition in MACHINES.values():
        for state in definition.states:
            names.add(f"{state.value}EnteredEvent")
            names.add(f"{state.value}ExitedEvent")
    return frozenset(names - _SUPPRESSED_STATE_EVENTS)


def all_event_names() -> frozenset[str]:
    """Every event name this platform can raise."""
    return state_event_names() | NON_STATE_EVENTS


@dataclass(frozen=True)
class Event:
    """One platform event."""

    name: str
    #: The object the event is about: a device, workstation, user or
    #: application name. Empty for platform-wide events.
    subject: str = ""
    #: The eventToken of the application this event belongs to. Subscribers
    #: presenting the same token receive it.
    event_token: str = ""
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
    )
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def is_state_event(self) -> bool:
        return self.name.endswith(("EnteredEvent", "ExitedEvent"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "eventName": self.name,
            "subject": self.subject,
            "eventToken": self.event_token,
            "at": self.at,
            **self.attributes,
        }

    @classmethod
    def from_state_change(
        cls, change: StateChange, *, event_token: str = ""
    ) -> Optional["Event"]:
        """Build the event for a state change, or ``None`` if suppressed."""
        if change.event_name in _SUPPRESSED_STATE_EVENTS:
            return None
        attributes: dict[str, Any] = {"machine": change.machine}
        if change.reason:
            attributes["reason"] = change.reason
        return cls(
            name=change.event_name,
            subject=change.subject,
            event_token=event_token,
            at=change.at,
            attributes=attributes,
        )


#: A subscriber receives events it is entitled to see.
Delivery = Callable[[Event], None]


@dataclass
class Subscription:
    """One application's interest in a token's events (section 31.1)."""

    subscriber_id: int
    #: The token whose events this subscription covers. Section 31.1 permits
    #: a token the platform has never seen.
    event_subscription_token: str
    deliver: Delivery = field(repr=False)

    def wants(self, event: Event) -> bool:
        return event.event_token == self.event_subscription_token


class EventBus:
    """Raises events and delivers them to entitled subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subscriptions: list[Subscription] = []
        self._listeners: list[Delivery] = []
        self._recent: list[Event] = []
        self._next_subscriber_id = 1

    # -- subscription (chapter 31) ----------------------------------------

    def subscribe(self, event_subscription_token: str, deliver: Delivery) -> int:
        """Add a subscription, returning its id.

        A token the platform has not yet seen is accepted: section 31.1
        requires it, so a management application can start first.
        """
        if not event_subscription_token:
            raise ValueError("eventSubscriptionToken must not be empty")
        with self._lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscriptions.append(
                Subscription(
                    subscriber_id=subscriber_id,
                    event_subscription_token=event_subscription_token,
                    deliver=deliver,
                )
            )
        log.debug(
            "subscription %d added for token %s",
            subscriber_id, event_subscription_token,
        )
        return subscriber_id

    def unsubscribe(self, event_subscription_token: str, *,
                    subscriber_id: Optional[int] = None) -> int:
        """Remove subscriptions for a token; returns how many were removed."""
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                subscription
                for subscription in self._subscriptions
                if not (
                    subscription.event_subscription_token
                    == event_subscription_token
                    and (
                        subscriber_id is None
                        or subscription.subscriber_id == subscriber_id
                    )
                )
            ]
            return before - len(self._subscriptions)

    def subscriptions(
        self, *, subscriber_id: Optional[int] = None
    ) -> list[str]:
        """Tokens currently subscribed, for ``<subscriptionListResponse>``."""
        with self._lock:
            return sorted(
                {
                    subscription.event_subscription_token
                    for subscription in self._subscriptions
                    if subscriber_id is None
                    or subscription.subscriber_id == subscriber_id
                }
            )

    def drop_subscriber(self, subscriber_id: int) -> int:
        """Remove every subscription of one session, when it disconnects."""
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [
                subscription
                for subscription in self._subscriptions
                if subscription.subscriber_id != subscriber_id
            ]
            return before - len(self._subscriptions)

    # -- listeners (logging, monitoring, the conformance recorder) --------

    def add_listener(self, listener: Delivery) -> None:
        """Register a listener that sees *every* event regardless of token.

        This is how the platform's own obligations are met -- section 10.3
        requires a log entry for entry to and exit from every device state,
        and a logger attached here satisfies that without the raising code
        having to know about it.
        """
        with self._lock:
            self._listeners.append(listener)

    # -- raising ----------------------------------------------------------

    def raise_event(self, event: Optional[Event]) -> Optional[Event]:
        """Publish an event to listeners and entitled subscribers.

        ``None`` is accepted and ignored so a caller can pass the result of
        :meth:`Event.from_state_change` directly.
        """
        if event is None:
            return None

        with self._lock:
            self._recent.append(event)
            if len(self._recent) > 1000:
                del self._recent[: len(self._recent) - 1000]
            listeners = list(self._listeners)
            targets = [s for s in self._subscriptions if s.wants(event)]

        for listener in listeners:
            self._safe_deliver(listener, event, "listener")
        for subscription in targets:
            self._safe_deliver(subscription.deliver, event, "subscriber")
        return event

    def raise_state_change(
        self, change: StateChange, *, event_token: str = ""
    ) -> Optional[Event]:
        return self.raise_event(
            Event.from_state_change(change, event_token=event_token)
        )

    @staticmethod
    def _safe_deliver(deliver: Delivery, event: Event, kind: str) -> None:
        try:
            deliver(event)
        except Exception:  # pragma: no cover - delivery target is caller code
            # A failing subscriber must never stop an event reaching the
            # others, nor unwind the state transition that raised it.
            log.exception("%s raised handling %s", kind, event.name)

    # -- inspection -------------------------------------------------------

    def recent(self, limit: int = 100) -> list[Event]:
        with self._lock:
            return list(self._recent[-limit:])

    @property
    def subscription_count(self) -> int:
        with self._lock:
            return len(self._subscriptions)


def attach_state_machine(
    bus: EventBus, machine, *, event_token: str = ""
) -> None:
    """Wire a state machine's transitions into the bus.

    Every entry and exit becomes a CUPPS event, except the platform's own
    stopped state, which raises none.
    """
    machine.add_listener(
        lambda change: bus.raise_state_change(change, event_token=event_token)
    )

"""Thin wrapper around paho-mqtt for the local Mosquitto broker.

Uses the v2 callback API (paho-mqtt >= 2.0). Handlers are registered per
topic; unknown topics are ignored. Reconnection is delegated to paho's
built-in loop with reconnect backoff.
"""

from __future__ import annotations

import logging
from typing import Callable

import paho.mqtt.client as mqtt

logger = logging.getLogger("gateway.mqtt")

Handler = Callable[[str, bytes], None]


class MqttBridge:
    def __init__(self, host: str = "localhost", port: int = 1883,
                 client_id: str = "spacebio-gateway") -> None:
        self._host = host
        self._port = port
        self._handlers: dict[str, Handler] = {}
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)

    def on(self, topic: str, handler: Handler) -> None:
        """Register a handler for a topic (subscribed on connect)."""
        self._handlers[topic] = handler

    def connect(self) -> None:
        logger.info("connecting to broker %s:%d", self._host, self._port)
        self._client.connect(self._host, self._port, keepalive=30)

    def publish(self, topic: str, payload: bytes,
                qos: int = 1, retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=qos, retain=retain)

    def loop_forever(self) -> None:
        self._client.loop_forever()

    def loop_start(self) -> None:
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    # -- callbacks -----------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error("broker connect failed: %s", reason_code)
            return
        for topic in self._handlers:
            client.subscribe(topic, qos=1)
            logger.info("subscribed %s", topic)

    def _on_message(self, client, userdata, msg):
        handler = self._handlers.get(msg.topic)
        if handler is None:
            return
        try:
            handler(msg.topic, msg.payload)
        except Exception:  # never let one bad message kill the loop
            logger.exception("handler error on %s", msg.topic)

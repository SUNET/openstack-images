"""Keystone notification listener for instant user-group sync.

Connects to RabbitMQ via aio-pika and listens for identity.user.created
oslo.messaging notifications. When a new user is created (e.g. federated
shadow user on first SSO login), triggers a callback to sync that user
into the appropriate project groups.
"""

import asyncio
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import aio_pika

logger = logging.getLogger(__name__)


async def start_notification_listener(
    transport_url: str,
    on_user_created: Callable[[str], Coroutine[Any, Any, None]],
) -> None:
    """Connect to RabbitMQ and listen for Keystone user creation events.

    Runs in a reconnect loop — on connection failure it retries every 10s.
    The loop exits cleanly on asyncio.CancelledError (operator shutdown).

    Args:
        transport_url: AMQP URL (amqp://user:pass@host:5672/vhost)
        on_user_created: async callback(user_id) called when a new user
            is created in Keystone.
    """
    while True:
        try:
            connection = await aio_pika.connect_robust(transport_url)
            async with connection:
                channel = await connection.channel()
                await channel.set_qos(prefetch_count=10)

                queue = await channel.declare_queue(
                    "openstack-operator-notifications",
                    durable=True,
                    arguments={"x-message-ttl": 300000},  # 5 min TTL
                )

                # oslo.messaging uses exchange = control_exchange (default "openstack")
                exchange = await channel.declare_exchange(
                    "openstack",
                    aio_pika.ExchangeType.TOPIC,
                    durable=True,
                )
                await queue.bind(exchange, routing_key="notifications.info")

                logger.info("Notification listener connected, consuming from queue")

                async for message in queue:
                    async with message.process():
                        try:
                            await _handle_message(message.body, on_user_created)
                        except Exception:
                            logger.exception("Failed to handle notification")

        except asyncio.CancelledError:
            logger.info("Notification listener shutting down")
            return
        except Exception:
            logger.exception(
                "Notification listener connection failed, retrying in 10s"
            )
            await asyncio.sleep(10)


async def _handle_message(
    body: bytes,
    on_user_created: Callable[[str], Coroutine[Any, Any, None]],
) -> None:
    """Parse oslo.messaging v2 notification and dispatch user creation events."""
    data = json.loads(body)

    # oslo.messaging v2 wraps the payload in "oslo.message"
    if "oslo.message" in data:
        inner = json.loads(data["oslo.message"])
    else:
        inner = data

    event_type = inner.get("event_type", "")

    if event_type != "identity.user.created":
        return

    payload = inner.get("payload", {})
    # CADF audit format: resource_info contains the user ID
    user_id = payload.get("resource_info", "")
    if not user_id:
        logger.warning("identity.user.created event with empty resource_info")
        return

    logger.info("Received identity.user.created for user_id=%s", user_id)
    await on_user_created(user_id)

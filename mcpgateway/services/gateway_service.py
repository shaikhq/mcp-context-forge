# -*- coding: utf-8 -*-
"""Gateway Service Implementation.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

This module implements gateway federation according to the MCP specification.
It handles:
- Gateway discovery and registration
- Request forwarding
- Capability aggregation
- Health monitoring
- Active/inactive gateway management
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Set

import httpx
from filelock import FileLock, Timeout
from mcp import ClientSession
from mcp.client.sse import sse_client
from sqlalchemy import select
from sqlalchemy.orm import Session

from mcpgateway.config import settings
from mcpgateway.db import Gateway as DbGateway
from mcpgateway.db import SessionLocal
from mcpgateway.db import Tool as DbTool
from mcpgateway.schemas import GatewayCreate, GatewayRead, GatewayUpdate, ToolCreate
from mcpgateway.services.tool_service import ToolService
from mcpgateway.utils.services_auth import decode_auth

try:
    import redis

    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logging.info("Redis is not utilized in this environment.")

# logging.getLogger("httpx").setLevel(logging.WARNING)  # Disables httpx logs for regular health checks
logger = logging.getLogger(__name__)


GW_FAILURE_THRESHOLD = settings.unhealthy_threshold
GW_HEALTH_CHECK_INTERVAL = settings.health_check_interval


class GatewayError(Exception):
    """Base class for gateway-related errors."""


class GatewayNotFoundError(GatewayError):
    """Raised when a requested gateway is not found."""


class GatewayNameConflictError(GatewayError):
    """Raised when a gateway name conflicts with existing (active or inactive) gateway."""

    def __init__(self, name: str, is_active: bool = True, gateway_id: Optional[int] = None):
        """Initialize the error with gateway information.

        Args:
            name: The conflicting gateway name
            is_active: Whether the existing gateway is active
            gateway_id: ID of the existing gateway if available
        """
        self.name = name
        self.is_active = is_active
        self.gateway_id = gateway_id
        message = f"Gateway already exists with name: {name}"
        if not is_active:
            message += f" (currently inactive, ID: {gateway_id})"
        super().__init__(message)


class GatewayConnectionError(GatewayError):
    """Raised when gateway connection fails."""


class GatewayService:
    """Service for managing federated gateways.

    Handles:
    - Gateway registration and health checks
    - Request forwarding
    - Capability negotiation
    - Federation events
    - Active/inactive status management
    """

    def __init__(self):
        """Initialize the gateway service."""
        self._event_subscribers: List[asyncio.Queue] = []
        self._http_client = httpx.AsyncClient(timeout=settings.federation_timeout, verify=not settings.skip_ssl_verify)
        self._health_check_interval = GW_HEALTH_CHECK_INTERVAL
        self._health_check_task: Optional[asyncio.Task] = None
        self._active_gateways: Set[str] = set()  # Track active gateway URLs
        self._stream_response = None
        self._pending_responses = {}
        self.tool_service = ToolService()
        self._gateway_failure_counts: dict[str, int] = {}

        # For health checks, we determine the leader instance.
        self.redis_url = settings.redis_url if settings.cache_type == "redis" else None

        if self.redis_url and REDIS_AVAILABLE:
            self._redis_client = redis.from_url(self.redis_url)
            self._instance_id = str(uuid.uuid4())  # Unique ID for this process
            self._leader_key = "gateway_service_leader"
            self._leader_ttl = 40  # seconds
        elif settings.cache_type != "none":
            # Fallback: File-based lock
            self._redis_client = None
            self._lock_path = settings.filelock_path
            self._file_lock = FileLock(self._lock_path)
        else:
            self._redis_client = None

    async def initialize(self) -> None:
        """Initialize the service and start health check if this instance is the leader.

        Raises:
            ConnectionError: When redis is not reachable
        """
        logger.info("Initializing gateway service")

        if self._redis_client:
            # Check if Redis is available
            pong = self._redis_client.ping()
            if not pong:
                raise ConnectionError("Redis ping failed.")

            is_leader = self._redis_client.set(self._leader_key, self._instance_id, ex=self._leader_ttl, nx=True)
            if is_leader:
                logger.info("Acquired Redis leadership. Starting health check task.")
                self._health_check_task = asyncio.create_task(self._run_health_checks())
        else:
            # Always create the health check task in filelock mode; leader check is handled inside.
            self._health_check_task = asyncio.create_task(self._run_health_checks())

    async def shutdown(self) -> None:
        """Shutdown the service."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        await self._http_client.aclose()
        self._event_subscribers.clear()
        self._active_gateways.clear()
        logger.info("Gateway service shutdown complete")

    async def register_gateway(self, db: Session, gateway: GatewayCreate) -> GatewayRead:
        """Register a new gateway.

        Args:
            db: Database session
            gateway: Gateway creation schema

        Returns:
            Created gateway information

        Raises:
            GatewayNameConflictError: If gateway name already exists
        """
        try:
            # Check for name conflicts (both active and inactive)
            existing_gateway = db.execute(select(DbGateway).where(DbGateway.name == gateway.name)).scalar_one_or_none()

            if existing_gateway:
                raise GatewayNameConflictError(
                    gateway.name,
                    is_active=existing_gateway.is_active,
                    gateway_id=existing_gateway.id,
                )

            auth_type = getattr(gateway, "auth_type", None)
            auth_value = getattr(gateway, "auth_value", {})

            capabilities, tools = await self._initialize_gateway(str(gateway.url), auth_value)

            all_names = [td.name for td in tools]

            existing_tools = db.execute(select(DbTool).where(DbTool.name.in_(all_names))).scalars().all()
            existing_tool_names = [tool.name for tool in existing_tools]

            tools = [
                DbTool(
                    name=tool.name,
                    url=str(gateway.url),
                    description=tool.description,
                    integration_type=tool.integration_type,
                    request_type=tool.request_type,
                    headers=tool.headers,
                    input_schema=tool.input_schema,
                    jsonpath_filter=tool.jsonpath_filter,
                    auth_type=auth_type,
                    auth_value=auth_value,
                )
                for tool in tools
            ]

            existing_tools = [tool for tool in tools if tool.name in existing_tool_names]
            new_tools = [tool for tool in tools if tool.name not in existing_tool_names]

            # Create DB model
            db_gateway = DbGateway(
                name=gateway.name,
                url=str(gateway.url),
                description=gateway.description,
                capabilities=capabilities,
                last_seen=datetime.now(timezone.utc),
                auth_type=auth_type,
                auth_value=auth_value,
                tools=new_tools,
                # federated_tools=existing_tools + new_tools
            )

            # Add to DB
            db.add(db_gateway)
            db.commit()
            db.refresh(db_gateway)

            # Update tracking
            self._active_gateways.add(db_gateway.url)

            # Notify subscribers
            await self._notify_gateway_added(db_gateway)

            return GatewayRead.model_validate(gateway)
        except* ValueError as ve:
            logger.error("ValueErrors in group: %s", ve.exceptions)
        except* RuntimeError as re:
            logger.error("RuntimeErrors in group: %s", re.exceptions)
        except* BaseException as other:  # catches every other sub-exception
            logger.error("Other grouped errors: %s", other.exceptions)
        # except IntegrityError as ex:
        #     logger.error(f"Error adding gateway: {ex}")
        #     db.rollback()
        #     raise GatewayError(f"Gateway already exists: {gateway.name}")
        # except Exception as e:
        #     db.rollback()
        #     raise GatewayError(f"Failed to register gateway: {str(e)}")

    async def list_gateways(self, db: Session, include_inactive: bool = False) -> List[GatewayRead]:
        """List all registered gateways.

        Args:
            db: Database session
            include_inactive: Whether to include inactive gateways

        Returns:
            List of registered gateways
        """
        query = select(DbGateway)

        if not include_inactive:
            query = query.where(DbGateway.is_active)

        gateways = db.execute(query).scalars().all()
        return [GatewayRead.model_validate(g) for g in gateways]

    async def update_gateway(self, db: Session, gateway_id: int, gateway_update: GatewayUpdate) -> GatewayRead:
        """Update a gateway.

        Args:
            db: Database session
            gateway_id: Gateway ID to update
            gateway_update: Updated gateway data

        Returns:
            Updated gateway information

        Raises:
            GatewayNotFoundError: If gateway not found
            GatewayError: For other update errors
            GatewayNameConflictError: If gateway name conflict occurs
        """
        try:
            # Find gateway
            gateway = db.get(DbGateway, gateway_id)
            if not gateway:
                raise GatewayNotFoundError(f"Gateway not found: {gateway_id}")

            if not gateway.is_active:
                raise GatewayNotFoundError(f"Gateway '{gateway.name}' exists but is inactive")

            # Check for name conflicts if name is being changed
            if gateway_update.name is not None and gateway_update.name != gateway.name:
                existing_gateway = db.execute(select(DbGateway).where(DbGateway.name == gateway_update.name).where(DbGateway.id != gateway_id)).scalar_one_or_none()

                if existing_gateway:
                    raise GatewayNameConflictError(
                        gateway_update.name,
                        is_active=existing_gateway.is_active,
                        gateway_id=existing_gateway.id,
                    )

            # Update fields if provided
            if gateway_update.name is not None:
                gateway.name = gateway_update.name
            if gateway_update.url is not None:
                gateway.url = str(gateway_update.url)
            if gateway_update.description is not None:
                gateway.description = gateway_update.description

            if getattr(gateway, "auth_type", None) is not None:
                gateway.auth_type = gateway_update.auth_type

                # if auth_type is not None and only then check auth_value
                if getattr(gateway, "auth_value", {}) != {}:
                    gateway.auth_value = gateway_update.auth_value

            # Try to reinitialize connection if URL changed
            if gateway_update.url is not None:
                try:
                    capabilities, _ = await self._initialize_gateway(gateway.url, gateway.auth_value)
                    gateway.capabilities = capabilities
                    gateway.last_seen = datetime.utcnow()

                    # Update tracking with new URL
                    self._active_gateways.discard(gateway.url)
                    self._active_gateways.add(gateway.url)
                except Exception as e:
                    logger.warning(f"Failed to initialize updated gateway: {e}")

            gateway.updated_at = datetime.utcnow()
            db.commit()
            db.refresh(gateway)

            # Notify subscribers
            await self._notify_gateway_updated(gateway)

            logger.info(f"Updated gateway: {gateway.name}")
            return GatewayRead.model_validate(gateway)

        except Exception as e:
            db.rollback()
            raise GatewayError(f"Failed to update gateway: {str(e)}")

    async def get_gateway(self, db: Session, gateway_id: int, include_inactive: bool = False) -> GatewayRead:
        """Get a specific gateway by ID.

        Args:
            db: Database session
            gateway_id: Gateway ID
            include_inactive: Whether to include inactive gateways

        Returns:
            Gateway information

        Raises:
            GatewayNotFoundError: If gateway not found
        """
        gateway = db.get(DbGateway, gateway_id)
        if not gateway:
            raise GatewayNotFoundError(f"Gateway not found: {gateway_id}")

        if not gateway.is_active and not include_inactive:
            raise GatewayNotFoundError(f"Gateway '{gateway.name}' exists but is inactive")

        return GatewayRead.model_validate(gateway)

    async def toggle_gateway_status(self, db: Session, gateway_id: int, activate: bool) -> GatewayRead:
        """Toggle gateway active status.

        Args:
            db: Database session
            gateway_id: Gateway ID to toggle
            activate: True to activate, False to deactivate

        Returns:
            Updated gateway information

        Raises:
            GatewayNotFoundError: If gateway not found
            GatewayError: For other errors
        """
        try:
            gateway = db.get(DbGateway, gateway_id)
            if not gateway:
                raise GatewayNotFoundError(f"Gateway not found: {gateway_id}")

            # Update status if it's different
            if gateway.is_active != activate:
                gateway.is_active = activate
                gateway.updated_at = datetime.utcnow()

                # Update tracking
                if activate:
                    self._active_gateways.add(gateway.url)
                    # Try to initialize if activating
                    try:
                        capabilities, tools = await self._initialize_gateway(gateway.url, gateway.auth_value)
                        gateway.capabilities = capabilities.dict()
                        gateway.last_seen = datetime.utcnow()
                    except Exception as e:
                        logger.warning(f"Failed to initialize reactivated gateway: {e}")
                else:
                    self._active_gateways.discard(gateway.url)

                db.commit()
                db.refresh(gateway)

                tools = db.query(DbTool).filter(DbTool.gateway_id == gateway_id).all()
                for tool in tools:
                    await self.tool_service.toggle_tool_status(db, tool.id, activate)

                # Notify subscribers
                if activate:
                    await self._notify_gateway_activated(gateway)
                else:
                    await self._notify_gateway_deactivated(gateway)

                logger.info(f"Gateway {gateway.name} {'activated' if activate else 'deactivated'}")

            return GatewayRead.model_validate(gateway)

        except Exception as e:
            db.rollback()
            raise GatewayError(f"Failed to toggle gateway status: {str(e)}")

    async def _notify_gateway_updated(self, gateway: DbGateway) -> None:
        """
        Notify subscribers of gateway update.

        Args:
            gateway: Gateway to update
        """
        event = {
            "type": "gateway_updated",
            "data": {
                "id": gateway.id,
                "name": gateway.name,
                "url": gateway.url,
                "description": gateway.description,
                "is_active": gateway.is_active,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._publish_event(event)

    async def delete_gateway(self, db: Session, gateway_id: int) -> None:
        """Permanently delete a gateway.

        Args:
            db: Database session
            gateway_id: Gateway ID to delete

        Raises:
            GatewayNotFoundError: If gateway not found
            GatewayError: For other deletion errors
        """
        try:
            # Find gateway
            gateway = db.get(DbGateway, gateway_id)
            if not gateway:
                raise GatewayNotFoundError(f"Gateway not found: {gateway_id}")

            # Store gateway info for notification before deletion
            gateway_info = {"id": gateway.id, "name": gateway.name, "url": gateway.url}

            # Hard delete gateway
            db.delete(gateway)
            db.commit()

            # Update tracking
            self._active_gateways.discard(gateway.url)

            # Notify subscribers
            await self._notify_gateway_deleted(gateway_info)

            logger.info(f"Permanently deleted gateway: {gateway.name}")

        except Exception as e:
            db.rollback()
            raise GatewayError(f"Failed to delete gateway: {str(e)}")

    async def forward_request(self, gateway: DbGateway, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Forward a request to a gateway.

        Args:
            gateway: Gateway to forward to
            method: RPC method name
            params: Optional method parameters

        Returns:
            Gateway response

        Raises:
            GatewayConnectionError: If forwarding fails
            GatewayError: If gateway gave an error
        """
        if not gateway.is_active:
            raise GatewayConnectionError(f"Cannot forward request to inactive gateway: {gateway.name}")

        try:
            # Build RPC request
            request = {"jsonrpc": "2.0", "id": 1, "method": method}
            if params:
                request["params"] = params

            # Directly use the persistent HTTP client (no async with)
            response = await self._http_client.post(f"{gateway.url}/rpc", json=request, headers=self._get_auth_headers())
            response.raise_for_status()
            result = response.json()

            # Update last seen timestamp
            gateway.last_seen = datetime.utcnow()

            if "error" in result:
                raise GatewayError(f"Gateway error: {result['error'].get('message')}")
            return result.get("result")

        except Exception as e:
            raise GatewayConnectionError(f"Failed to forward request to {gateway.name}: {str(e)}")

    async def _handle_gateway_failure(self, gateway: str) -> None:
        """
        Tracks and handles gateway failures during health checks.
        If the failure count exceeds the threshold, the gateway is deactivated.

        Args:
            gateway (str): The gateway object that failed its health check.

        Returns:
            None
        """
        if GW_FAILURE_THRESHOLD == -1:
            return  # Gateway failure action disabled
        count = self._gateway_failure_counts.get(gateway.id, 0) + 1
        self._gateway_failure_counts[gateway.id] = count

        logger.warning(f"Gateway {gateway.name} failed health check {count} time(s).")

        if count >= GW_FAILURE_THRESHOLD:
            logger.error(f"Gateway {gateway.name} failed {GW_FAILURE_THRESHOLD} times. Deactivating...")
            with SessionLocal() as db:
                await self.toggle_gateway_status(db, gateway.id, False)
                self._gateway_failure_counts[gateway.id] = 0  # Reset after deactivation

    async def check_health_of_gateways(self, gateways: List[DbGateway]) -> bool:
        """Health check for a list of gateways.

        Deactivates gateway if gateway is not healthy.

        Args:
            gateways (List[DbGateway]): List of gateways to check if healthy

        Returns:
            bool: True if all  active gateways are healthy
        """
        # Reuse a single HTTP client for all requests
        async with httpx.AsyncClient() as client:
            for gateway in gateways:
                # Inactive gateways are unhealthy
                if not gateway.is_active:
                    continue

                try:
                    # Ensure auth_value is a dict
                    auth_data = gateway.auth_value or {}
                    headers = decode_auth(auth_data)

                    # Perform the GET and raise on 4xx/5xx
                    async with client.stream("GET", gateway.url, headers=headers) as response:
                        # This will raise immediately if status is 4xx/5xx
                        response.raise_for_status()

                    # Mark successful check
                    gateway.last_seen = datetime.utcnow()

                except Exception:
                    await self._handle_gateway_failure(gateway)

        # All gateways passed
        return True

    async def aggregate_capabilities(self, db: Session) -> Dict[str, Any]:
        """Aggregate capabilities from all gateways.

        Args:
            db: Database session

        Returns:
            Combined capabilities
        """
        capabilities = {
            "prompts": {"listChanged": True},
            "resources": {"subscribe": True, "listChanged": True},
            "tools": {"listChanged": True},
            "logging": {},
        }

        # Get all active gateways
        gateways = db.execute(select(DbGateway).where(DbGateway.is_active)).scalars().all()

        # Combine capabilities
        for gateway in gateways:
            if gateway.capabilities:
                for key, value in gateway.capabilities.items():
                    if key not in capabilities:
                        capabilities[key] = value
                    elif isinstance(value, dict):
                        capabilities[key].update(value)

        return capabilities

    async def subscribe_events(self) -> AsyncGenerator[Dict[str, Any], None]:
        """Subscribe to gateway events.

        Yields:
            Gateway event messages
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._event_subscribers.append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._event_subscribers.remove(queue)

    async def _initialize_gateway(self, url: str, authentication: Optional[Dict[str, str]] = None) -> Any:
        """Initialize connection to a gateway and retrieve its capabilities.

        Args:
            url: Gateway URL
            authentication: Optional authentication headers

        Returns:
            Capabilities dictionary as provided by the gateway.

        Raises:
            GatewayConnectionError: If initialization fails.
        """
        try:
            if authentication is None:
                authentication = {}

            async def connect_to_sse_server(server_url: str, authentication: Optional[Dict[str, str]] = None):
                """
                Connect to an MCP server running with SSE transport

                Args:
                    server_url: URL to connect to the server
                    authentication: Authentication headers for connection to URL

                Returns:
                    list, list: List of capabilities and tools
                """
                if authentication is None:
                    authentication = {}
                # Store the context managers so they stay alive
                decoded_auth = decode_auth(authentication)

                # Use async with for both sse_client and ClientSession
                async with sse_client(url=server_url, headers=decoded_auth) as streams:
                    async with ClientSession(*streams) as session:
                        # Initialize the session
                        response = await session.initialize()
                        capabilities = response.capabilities.model_dump(by_alias=True, exclude_none=True)

                        response = await session.list_tools()
                        tools = response.tools
                        tools = [tool.model_dump(by_alias=True, exclude_none=True) for tool in tools]
                        tools = [ToolCreate.model_validate(tool) for tool in tools]

                return capabilities, tools

            capabilities, tools = await connect_to_sse_server(url, authentication)

            return capabilities, tools
        except Exception as e:
            raise GatewayConnectionError(f"Failed to initialize gateway at {url}: {str(e)}")

    def _get_active_gateways(self) -> list[DbGateway]:
        """Sync function for database operations (runs in thread).

        Returns:
            List[DbGateway]: List of active gateways
        """
        with SessionLocal() as db:
            return db.execute(select(DbGateway).where(DbGateway.is_active)).scalars().all()

    async def _run_health_checks(self) -> None:
        """Run health checks periodically,
        Uses Redis or FileLock - for multiple workers.
        Uses simple health check for single worker mode."""

        while True:
            try:
                if self._redis_client and settings.cache_type == "redis":
                    # Redis-based leader check
                    current_leader = self._redis_client.get(self._leader_key)
                    if current_leader != self._instance_id.encode():
                        return
                    self._redis_client.expire(self._leader_key, self._leader_ttl)

                    # Run health checks
                    gateways = await asyncio.to_thread(self._get_active_gateways)
                    if gateways:
                        await self.check_health_of_gateways(gateways)

                    await asyncio.sleep(self._health_check_interval)

                elif settings.cache_type == "none":
                    try:
                        # For single worker mode, run health checks directly
                        gateways = await asyncio.to_thread(self._get_active_gateways)

                        if gateways:
                            await self.check_health_of_gateways(gateways)
                    except Exception as e:
                        logger.error(f"Health check run failed: {str(e)}")

                    await asyncio.sleep(self._health_check_interval)

                else:
                    # FileLock-based leader fallback
                    try:
                        self._file_lock.acquire(timeout=0)
                        logger.info("File lock acquired. Running health checks.")

                        while True:
                            gateways = await asyncio.to_thread(self._get_active_gateways)
                            if gateways:
                                await self.check_health_of_gateways(gateways)
                            await asyncio.sleep(self._health_check_interval)

                    except Timeout:
                        logger.debug("File lock already held. Retrying later.")
                        await asyncio.sleep(self._health_check_interval)

                    except Exception as e:
                        logger.error(f"FileLock health check failed: {str(e)}")

                    finally:
                        if self._file_lock.is_locked:
                            try:
                                self._file_lock.release()
                                logger.info("Released file lock.")
                            except Exception as e:
                                logger.warning(f"Failed to release file lock: {str(e)}")

            except Exception as e:
                logger.error(f"Unexpected error in health check loop: {str(e)}")
                await asyncio.sleep(self._health_check_interval)

    def _get_auth_headers(self) -> Dict[str, str]:
        """
        Get headers for gateway authentication.

        Returns:
            dict: Authorization header dict
        """
        api_key = f"{settings.basic_auth_user}:{settings.basic_auth_password}"
        return {"Authorization": f"Basic {api_key}", "X-API-Key": api_key, "Content-Type": "application/json"}

    async def _notify_gateway_added(self, gateway: DbGateway) -> None:
        """
        Notify subscribers of gateway addition.

        Args:
            gateway: Gateway to add
        """
        event = {
            "type": "gateway_added",
            "data": {
                "id": gateway.id,
                "name": gateway.name,
                "url": gateway.url,
                "description": gateway.description,
                "is_active": gateway.is_active,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._publish_event(event)

    async def _notify_gateway_activated(self, gateway: DbGateway) -> None:
        """
        Notify subscribers of gateway activation.

        Args:
            gateway: Gateway to activate
        """
        event = {
            "type": "gateway_activated",
            "data": {
                "id": gateway.id,
                "name": gateway.name,
                "url": gateway.url,
                "is_active": True,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._publish_event(event)

    async def _notify_gateway_deactivated(self, gateway: DbGateway) -> None:
        """
        Notify subscribers of gateway deactivation.

        Args:
            gateway: Gateway database object
        """
        event = {
            "type": "gateway_deactivated",
            "data": {
                "id": gateway.id,
                "name": gateway.name,
                "url": gateway.url,
                "is_active": False,
            },
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._publish_event(event)

    async def _notify_gateway_deleted(self, gateway_info: Dict[str, Any]) -> None:
        """
        Notify subscribers of gateway deletion.

        Args:
            gateway_info: Dict containing information about gateway to delete
        """
        event = {
            "type": "gateway_deleted",
            "data": gateway_info,
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._publish_event(event)

    async def _notify_gateway_removed(self, gateway: DbGateway) -> None:
        """
        Notify subscribers of gateway removal (deactivation).

        Args:
            gateway: Gateway to remove
        """
        event = {
            "type": "gateway_removed",
            "data": {"id": gateway.id, "name": gateway.name, "is_active": False},
            "timestamp": datetime.utcnow().isoformat(),
        }
        await self._publish_event(event)

    async def _publish_event(self, event: Dict[str, Any]) -> None:
        """
        Publish event to all subscribers.

        Args:
            event: event dictionary
        """
        for queue in self._event_subscribers:
            await queue.put(event)

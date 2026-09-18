"""API client for the OBI / heyOBI Energy Tracking backend.

The JWT obtained on login is only ever kept in memory on this client. It is
never logged, never persisted, and never exposed to entities or attributes.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import email.utils
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from .const import (
    ACCEPT_ANALYTICS_FORECAST,
    ACCEPT_ANALYTICS_STANDBY,
    ACCEPT_BRIDGES,
    ACCEPT_HISTORICAL,
    ACCEPT_LANGUAGE,
    ACCEPT_SENSOR,
    ANALYTICS_FORECAST_URL_TEMPLATE,
    ANALYTICS_STANDBY_URL_TEMPLATE,
    API_KEY,
    BRIDGES_URL,
    HISTORICAL_DATA_URL_TEMPLATE,
    LOGIN_COOKIE,
    LOGIN_COUNTRY,
    LOGIN_HOST,
    LOGIN_ORIGIN,
    LOGIN_REFERER,
    LOGIN_URL,
    LIVE_DATA_URL,
    LIVE_USER_AGENT,
    SENSOR_URL_TEMPLATE,
    USER_AGENT,
)

_LOGGER = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_MAX_LOG_BODY_CHARS = 300

# Login backoff. OBI's auth endpoint (www.obi.de/regi/auth) answers a
# throttled client with a bare 401 (empty body) before it escalates to a
# real 429, so a failed login must never be retried immediately.
_LOGIN_BACKOFF_BASE = 60
_LOGIN_BACKOFF_MAX = 3600
# Fall back to this when a 429 carries no (parsable) Retry-After header.
_DEFAULT_RETRY_AFTER = 300
# Renew a JWT this long before its own "exp" claim runs out.
_TOKEN_EXPIRY_MARGIN = timedelta(minutes=5)
# Response headers worth logging. Everything else (notably Set-Cookie) is
# dropped so no session material can end up in logs or issue attachments.
_LOGGED_HEADERS = (
    "Retry-After",
    "Date",
    "X-Cache",
    "X-Amz-Cf-Id",
    "Content-Length",
    "Content-Type",
)


class ObiApiError(Exception):
    """Base exception for OBI API errors."""


class ObiAuthError(ObiApiError):
    """Raised when authentication fails (bad credentials or expired session)."""


class ObiConnectionError(ObiApiError):
    """Raised on network or unexpected HTTP errors."""


class ObiNotFoundError(ObiApiError):
    """Raised when a resource (e.g. /bridges) returns 404."""


class ObiRateLimitError(ObiApiError):
    """Raised when OBI throttles us (HTTP 429, or a login in cooldown).

    `retry_after` is the number of seconds to stay away, taken from the
    server's Retry-After header when it sends one.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Store the message and the server-requested cooldown."""
        super().__init__(message)
        self.retry_after = retry_after


_ISO8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def _parse_iso8601_duration(duration: str) -> timedelta:
    """Parse a simple ISO 8601 duration (e.g. PT6H, P1D) into a timedelta."""
    match = _ISO8601_DURATION_RE.match(duration.strip())
    if not match or not any(match.groups()):
        raise ValueError(f"Unsupported ISO 8601 duration: {duration!r}")
    parts = {key: int(value) for key, value in match.groupdict(default="0").items()}
    return timedelta(
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )


def _parse_retry_after(headers: Any) -> float:
    """Return the Retry-After delay in seconds, or a safe default.

    RFC 7231 allows either delta-seconds or an HTTP-date; accept both and
    fall back to a conservative default for a missing or malformed value.
    """
    raw = headers.get("Retry-After")
    if not raw:
        return _DEFAULT_RETRY_AFTER
    raw = raw.strip()

    try:
        return max(0.0, float(int(raw)))
    except ValueError:
        pass

    try:
        parsed = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        # parsedate_to_datetime raises on malformed input (Python >= 3.10).
        _LOGGER.debug("Could not parse Retry-After value %r", raw)
        return _DEFAULT_RETRY_AFTER
    if parsed is None:
        _LOGGER.debug("Could not parse Retry-After value %r", raw)
        return _DEFAULT_RETRY_AFTER
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def _jwt_expiry(token: str) -> datetime | None:
    """Return the JWT's own expiry, or None if it cannot be read.

    Only the payload is decoded (no signature check) - this is our own
    token and we merely want to know how long OBI considers it valid,
    instead of guessing with a fixed refresh interval.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_segment = parts[1]
    payload_segment += "=" * (-len(payload_segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_segment))
    except (ValueError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _truncate(text: str | None) -> str:
    """Truncate a response body to a safe length for logging."""
    if not text:
        return "<empty body>"
    text = text.strip()
    if len(text) > _MAX_LOG_BODY_CHARS:
        return text[:_MAX_LOG_BODY_CHARS] + "... [truncated]"
    return text


async def _safe_text(resp: aiohttp.ClientResponse) -> str:
    """Best-effort read of a response body for logging. Never raises."""
    try:
        return await resp.text()
    except (aiohttp.ClientError, UnicodeDecodeError):
        return "<could not read body>"


async def _log_http_error(resp: aiohttp.ClientResponse, context: str) -> None:
    """Log HTTP status, selected response headers and a truncated body.

    Only a whitelist of the server's *response* headers is logged. Request
    headers are never logged, and Set-Cookie is deliberately excluded, so no
    cookie, token or password can end up in logs or issue attachments.
    """
    body = await _safe_text(resp)
    headers = {
        name: value
        for name, value in resp.headers.items()
        if name.title() in _LOGGED_HEADERS
    }
    _LOGGER.error(
        "%s failed with HTTP %s. Response headers: %s. Response body: %s",
        context,
        resp.status,
        headers,
        _truncate(body),
    )


class ObiApiClient:
    """Thin async client for the OBI Energy Tracking API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        email: str,
        password: str,
        login_refresh_interval: int,
    ) -> None:
        """Initialize the client. The token is kept only in memory."""
        self._session = session
        self._email = email
        self._password = password
        self._login_refresh_interval = timedelta(seconds=login_refresh_interval)
        self._token: str | None = None
        self._token_obtained_at: datetime | None = None
        self._token_expires_at: datetime | None = None
        # Serializes logins so one poll cycle (7 requests) can never trigger
        # more than a single login, and holds the throttling backoff state.
        self._login_lock = asyncio.Lock()
        self._login_blocked_until: datetime | None = None
        self._login_failures = 0

    def update_credentials(self, email: str, password: str) -> None:
        """Update the credentials used for future logins."""
        self._email = email
        self._password = password
        self._token = None
        self._token_obtained_at = None
        self._token_expires_at = None
        # New credentials deserve a fresh attempt: drop any active cooldown.
        self._login_blocked_until = None
        self._login_failures = 0

    def update_login_refresh_interval(self, login_refresh_interval: int) -> None:
        """Update how often the token is proactively refreshed."""
        self._login_refresh_interval = timedelta(seconds=login_refresh_interval)

    @property
    def is_authenticated(self) -> bool:
        """Return whether a token is currently held in memory."""
        return self._token is not None

    def _token_is_stale(self) -> bool:
        if self._token is None or self._token_obtained_at is None:
            return True

        now = datetime.now(timezone.utc)
        # The token states its own lifetime; trust that over the configured
        # interval so a long-lived JWT is not replaced (and re-logged-in) every
        # 55 minutes for no reason. Every login is a chance to get throttled.
        if self._token_expires_at is not None:
            return now >= self._token_expires_at - _TOKEN_EXPIRY_MARGIN

        return now - self._token_obtained_at >= self._login_refresh_interval

    def _remaining_cooldown(self) -> float:
        """Return seconds left on the login cooldown (0 if not blocked)."""
        if self._login_blocked_until is None:
            return 0.0
        remaining = (
            self._login_blocked_until - datetime.now(timezone.utc)
        ).total_seconds()
        return max(0.0, remaining)

    def _register_login_failure(self, retry_after: float | None = None) -> None:
        """Start (or extend) the login cooldown after a rejected login.

        Only auth-style rejections get here - a timeout or a DNS hiccup must
        not lock the integration out, so those leave the counter alone.
        """
        self._login_failures += 1
        backoff = min(
            _LOGIN_BACKOFF_BASE * 2 ** (self._login_failures - 1),
            _LOGIN_BACKOFF_MAX,
        )
        # A server-stated Retry-After wins whenever it asks for more patience.
        delay = max(backoff, retry_after or 0.0)
        self._login_blocked_until = datetime.now(timezone.utc) + timedelta(
            seconds=delay
        )
        _LOGGER.warning(
            "OBI login rejected (%s consecutive failure(s)); not retrying for %.0f s",
            self._login_failures,
            delay,
        )

    async def async_login(self) -> None:
        """Log in to OBI, honouring the throttling cooldown.

        OBI's auth endpoint answers a throttled client with a bare 401 before
        it escalates to 429, and the rejection appears to stick to the account
        rather than only the IP. Retrying such a login immediately makes the
        lockout worse, so failures put the client into an exponential cooldown
        and further attempts are refused locally until it lapses.
        """
        remaining = self._remaining_cooldown()
        if remaining > 0:
            raise ObiRateLimitError(
                f"OBI login is in cooldown for another {remaining:.0f} s "
                f"after {self._login_failures} failed attempt(s)",
                retry_after=remaining,
            )

        await self._async_login_request()

    async def _async_login_request(self) -> None:
        """Perform the actual login request and store the resulting JWT."""
        # Serialize the body ourselves - compact, no whitespace - and send it
        # via `data=` (like the previously working YAML REST sensor did),
        # instead of aiohttp's `json=` shortcut, which re-derives its own
        # content-type/content-length handling and can conflict with the
        # exact headers OBI (and the CloudFront in front of it) expect.
        payload = json.dumps(
            {
                "password": self._password,
                "country": LOGIN_COUNTRY,
                "email": self._email,
            },
            separators=(",", ":"),
        )
        payload_bytes = payload.encode("utf-8")

        headers = {
            "content-type": "application/json",
            "accept": "*/*",
            "user-agent": USER_AGENT,
            "accept-language": ACCEPT_LANGUAGE,
            "accept-encoding": "identity",
            "cookie": LOGIN_COOKIE,
            "content-length": str(len(payload_bytes)),
            "host": LOGIN_HOST,
            "origin": LOGIN_ORIGIN,
            "referer": LOGIN_REFERER,
        }
        _LOGGER.debug("Logging in to OBI (%s)", LOGIN_URL)

        try:
            async with self._session.post(
                LOGIN_URL,
                data=payload_bytes,
                headers=headers,
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    retry_after = _parse_retry_after(resp.headers)
                    await _log_http_error(resp, "OBI login")
                    self._register_login_failure(retry_after)
                    raise ObiRateLimitError(
                        "OBI login was rate limited (HTTP 429)",
                        retry_after=retry_after,
                    )
                if resp.status in (401, 403):
                    await _log_http_error(resp, "OBI login")
                    # Indistinguishable from throttling: OBI returns an empty
                    # 401 both for a genuinely wrong password and for a client
                    # it has decided to shut out. Back off either way.
                    self._register_login_failure()
                    raise ObiAuthError(
                        f"Login failed with HTTP {resp.status}: invalid credentials"
                    )
                if resp.status >= 400:
                    await _log_http_error(resp, "OBI login")
                    raise ObiConnectionError(
                        f"Login request failed with HTTP {resp.status}"
                    )
                try:
                    data = await resp.json(content_type=None)
                except ValueError as err:
                    text = await _safe_text(resp)
                    _LOGGER.error(
                        "OBI login returned invalid JSON (HTTP %s): %s",
                        resp.status,
                        _truncate(text),
                    )
                    raise ObiConnectionError(
                        "Received invalid response from OBI login"
                    ) from err
        except aiohttp.ClientConnectorDNSError as err:
            _LOGGER.error("DNS resolution failed while logging in to OBI: %s", err)
            raise ObiConnectionError(
                "DNS resolution failed for the OBI login endpoint"
            ) from err
        except aiohttp.ClientSSLError as err:
            _LOGGER.error("SSL/TLS error while logging in to OBI: %s", err)
            raise ObiConnectionError(
                "SSL/TLS error while connecting to the OBI login endpoint"
            ) from err
        except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as err:
            _LOGGER.error("Timeout while logging in to OBI: %s", err)
            raise ObiConnectionError(
                "Timeout while connecting to the OBI login endpoint"
            ) from err
        except aiohttp.ClientConnectorError as err:
            _LOGGER.error("Could not connect to the OBI login endpoint: %s", err)
            raise ObiConnectionError(
                "Could not connect to the OBI login endpoint"
            ) from err
        except aiohttp.ClientError as err:
            _LOGGER.error("Network error while logging in to OBI: %s", err)
            raise ObiConnectionError("Network error during OBI login") from err

        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            _LOGGER.error("OBI login response did not contain a token")
            self._register_login_failure()
            raise ObiAuthError("Login response did not contain a token")

        self._token = token
        self._token_obtained_at = datetime.now(timezone.utc)
        self._token_expires_at = _jwt_expiry(token)
        self._login_failures = 0
        self._login_blocked_until = None
        _LOGGER.debug(
            "OBI login succeeded (token valid until %s)",
            self._token_expires_at.isoformat()
            if self._token_expires_at
            else "unknown",
        )

    def _api_headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "x-api-key": API_KEY,
            "x-app-type": "b2c",
            "Accept": accept,
            "accept-language": ACCEPT_LANGUAGE,
            "user-agent": USER_AGENT,
            # OBI's API is fronted by CloudFront. Ask it (and any
            # intermediate cache) to not serve a stale cached response for
            # historical-data polls, which otherwise can appear to "stop
            # updating" even though fresh readings exist upstream.
            "cache-control": "no-cache",
            "pragma": "no-cache",
        }

    async def _ensure_logged_in(self) -> None:
        """Log in if the current token is stale, at most once concurrently."""
        if not self._token_is_stale():
            return

        # Re-check inside the lock: a poll cycle fires several requests, and
        # without this every one of them would queue up its own login.
        async with self._login_lock:
            if self._token_is_stale():
                await self.async_login()

    async def _async_relogin(self) -> None:
        """Force a fresh login after a 401, at most once concurrently.

        If a concurrent caller already replaced the token while we waited for
        the lock, reuse that one instead of logging in a second time.
        """
        stale_token = self._token
        async with self._login_lock:
            if self._token is not None and self._token != stale_token:
                return
            await self.async_login()

    async def _authenticated_get(
        self, url: str, *, accept: str, params: dict[str, Any] | None = None
    ) -> Any:
        await self._ensure_logged_in()

        for attempt in range(2):
            headers = self._api_headers(accept)
            try:
                async with self._session.get(
                    url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers)
                        await _log_http_error(resp, f"OBI request to {url}")
                        raise ObiRateLimitError(
                            f"Request to {url} was rate limited (HTTP 429)",
                            retry_after=retry_after,
                        )
                    if resp.status == 401:
                        if attempt == 0:
                            _LOGGER.debug(
                                "OBI API returned 401 for %s, refreshing token and retrying",
                                url,
                            )
                            await self._async_relogin()
                            continue
                        await _log_http_error(resp, f"OBI request to {url}")
                        raise ObiAuthError("Not authorized after refreshing token")
                    if resp.status == 404:
                        _LOGGER.warning("OBI resource not found (HTTP 404): %s", url)
                        raise ObiNotFoundError(f"Resource not found: {url}")
                    if resp.status >= 400:
                        await _log_http_error(resp, f"OBI request to {url}")
                        raise ObiConnectionError(
                            f"Request to {url} failed with HTTP {resp.status}"
                        )
                    try:
                        return await resp.json(content_type=None)
                    except ValueError as err:
                        text = await _safe_text(resp)
                        _LOGGER.error(
                            "OBI response for %s was not valid JSON (HTTP %s): %s",
                            url,
                            resp.status,
                            _truncate(text),
                        )
                        raise ObiConnectionError(
                            f"Received invalid response from {url}"
                        ) from err
            except aiohttp.ClientConnectorDNSError as err:
                _LOGGER.error("DNS resolution failed requesting %s: %s", url, err)
                raise ObiConnectionError(f"DNS resolution failed for {url}") from err
            except aiohttp.ClientSSLError as err:
                _LOGGER.error("SSL/TLS error requesting %s: %s", url, err)
                raise ObiConnectionError(f"SSL/TLS error requesting {url}") from err
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as err:
                _LOGGER.error("Timeout requesting %s: %s", url, err)
                raise ObiConnectionError(f"Timeout requesting {url}") from err
            except aiohttp.ClientConnectorError as err:
                _LOGGER.error("Could not connect to %s: %s", url, err)
                raise ObiConnectionError(f"Could not connect to {url}") from err
            except aiohttp.ClientError as err:
                _LOGGER.error("Network error requesting %s: %s", url, err)
                raise ObiConnectionError(f"Network error requesting {url}") from err

        raise ObiAuthError("Not authorized after refreshing token")

    async def async_get_bridges(self) -> list[dict[str, Any]]:
        """Return the list of bridges (households) with their sensors."""
        data = await self._authenticated_get(BRIDGES_URL, accept=ACCEPT_BRIDGES)
        if not isinstance(data, list):
            _LOGGER.error(
                "Unexpected response type for /bridges: %s", type(data).__name__
            )
            raise ObiConnectionError("Unexpected response format for /bridges")
        return data

    async def async_get_historical_data(
        self, hh_id: str, mid_id: str, duration: str
    ) -> list[dict[str, Any]]:
        """Return historical measurements for the given bridge/sensor."""
        url = HISTORICAL_DATA_URL_TEMPLATE.format(hh_id=hh_id, mid_id=mid_id)

        try:
            delta = _parse_iso8601_duration(duration)
        except ValueError as err:
            _LOGGER.error("Invalid historical duration %r: %s", duration, err)
            raise ObiConnectionError(f"Invalid historical duration: {duration}") from err

        # OBI's API expects a single ISO 8601 time interval
        # (<start>/<duration>), not separate end/duration parameters.
        start = datetime.now(timezone.utc) - delta
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        params = {
            "duration": f"{start_str}/{duration}",
            "measures": "energy,negative_energy",
        }
        data = await self._authenticated_get(url, accept=ACCEPT_HISTORICAL, params=params)
        if not isinstance(data, list):
            _LOGGER.error(
                "Unexpected response type for historical data: %s", type(data).__name__
            )
            raise ObiConnectionError("Unexpected response format for historical data")
        return data

    async def async_get_consumption_forecast(
        self, hh_id: str, mid_id: str
    ) -> dict[str, Any]:
        """Return weekly/monthly consumption forecast for the given bridge/sensor."""
        url = ANALYTICS_FORECAST_URL_TEMPLATE.format(hh_id=hh_id, mid_id=mid_id)
        data = await self._authenticated_get(url, accept=ACCEPT_ANALYTICS_FORECAST)
        if not isinstance(data, dict):
            _LOGGER.error(
                "Unexpected response type for forecast: %s", type(data).__name__
            )
            raise ObiConnectionError("Unexpected response format for consumption forecast")
        return data

    async def async_get_standby_power(
        self, hh_id: str, mid_id: str, interval: str, duration: str
    ) -> list[dict[str, Any]]:
        """Return average standby power records (watts) for a completed-period interval."""
        url = ANALYTICS_STANDBY_URL_TEMPLATE.format(
            hh_id=hh_id, mid_id=mid_id, interval=interval
        )

        try:
            delta = _parse_iso8601_duration(duration)
        except ValueError as err:
            _LOGGER.error("Invalid standby duration %r: %s", duration, err)
            raise ObiConnectionError(f"Invalid standby duration: {duration}") from err

        start = datetime.now(timezone.utc) - delta
        start_str = start.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        params = {"duration": f"{start_str}/{duration}"}
        data = await self._authenticated_get(url, accept=ACCEPT_ANALYTICS_STANDBY, params=params)
        if not isinstance(data, list):
            _LOGGER.error(
                "Unexpected response type for standby (%s): %s", interval, type(data).__name__
            )
            raise ObiConnectionError(
                f"Unexpected response format for standby power ({interval})"
            )
        return data

    async def async_set_sensor_upload_interval(
        self, mid_id: str, upload_interval: int
    ) -> dict[str, Any]:
        """Set the upload interval for a sensor and return the updated sensor."""
        await self._ensure_logged_in()

        url = SENSOR_URL_TEMPLATE.format(mid_id=mid_id)
        payload = json.dumps(
            {"id": mid_id, "uploadInterval": upload_interval},
            separators=(",", ":"),
        )
        payload_bytes = payload.encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": ACCEPT_SENSOR,
            "Accept-Language": ACCEPT_LANGUAGE,
            "Content-Type": ACCEPT_SENSOR,
            "Content-Length": str(len(payload_bytes)),
            "User-Agent": LIVE_USER_AGENT,
            "X-Platform": "iOS",
            "X-Lib-Version": "26.6.9",
        }

        for attempt in range(2):
            try:
                async with self._session.patch(
                    url,
                    data=payload_bytes,
                    headers=headers,
                    timeout=_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status == 429:
                        retry_after = _parse_retry_after(resp.headers)
                        await _log_http_error(resp, f"OBI sensor update to {url}")
                        raise ObiRateLimitError(
                            "OBI sensor update was rate limited (HTTP 429)",
                            retry_after=retry_after,
                        )
                    if resp.status == 401 and attempt == 0:
                        _LOGGER.debug(
                            "OBI sensor update returned 401, refreshing token and retrying"
                        )
                        await self._async_relogin()
                        headers["Authorization"] = f"Bearer {self._token}"
                        continue
                    if resp.status in (401, 403):
                        await _log_http_error(resp, f"OBI sensor update to {url}")
                        raise ObiAuthError(
                            f"Sensor update failed with HTTP {resp.status}"
                        )
                    if resp.status == 404:
                        _LOGGER.warning("OBI sensor not found (HTTP 404): %s", url)
                        raise ObiNotFoundError(f"Sensor not found: {url}")
                    if resp.status >= 400:
                        await _log_http_error(resp, f"OBI sensor update to {url}")
                        raise ObiConnectionError(
                            f"Sensor update to {url} failed with HTTP {resp.status}"
                        )
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError as err:
                        text = await _safe_text(resp)
                        _LOGGER.error(
                            "OBI sensor update response was not valid JSON (HTTP %s): %s",
                            resp.status,
                            _truncate(text),
                        )
                        raise ObiConnectionError(
                            "Received invalid response from OBI sensor update"
                        ) from err
                    if not isinstance(data, dict):
                        _LOGGER.error(
                            "Unexpected response type for sensor update: %s",
                            type(data).__name__,
                        )
                        raise ObiConnectionError(
                            "Unexpected response format for sensor update"
                        )
                    return data
            except aiohttp.ClientConnectorDNSError as err:
                _LOGGER.error("DNS resolution failed updating %s: %s", url, err)
                raise ObiConnectionError(f"DNS resolution failed for {url}") from err
            except aiohttp.ClientSSLError as err:
                _LOGGER.error("SSL/TLS error updating %s: %s", url, err)
                raise ObiConnectionError(f"SSL/TLS error updating {url}") from err
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as err:
                _LOGGER.error("Timeout updating %s: %s", url, err)
                raise ObiConnectionError(f"Timeout updating {url}") from err
            except aiohttp.ClientConnectorError as err:
                _LOGGER.error("Could not connect to %s: %s", url, err)
                raise ObiConnectionError(f"Could not connect to {url}") from err
            except aiohttp.ClientError as err:
                _LOGGER.error("Network error updating %s: %s", url, err)
                raise ObiConnectionError(f"Network error updating {url}") from err

        raise ObiAuthError("Sensor update was not authorized after refreshing token")

    async def async_connect_live_data(
        self, hh_id: str, mid_id: str
    ) -> aiohttp.ClientWebSocketResponse:
        """Open the live-data WebSocket for the given bridge/sensor."""
        await self._ensure_logged_in()

        params = {
            "bridgeId": hh_id,
            "sensorId": mid_id,
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "*/*",
            "Accept-Language": ACCEPT_LANGUAGE,
            "User-Agent": LIVE_USER_AGENT,
            "X-Platform": "iOS",
            "X-Lib-Version": "26.6.9",
        }

        for attempt in range(2):
            try:
                return await self._session.ws_connect(
                    LIVE_DATA_URL,
                    params=params,
                    headers=headers,
                    timeout=_REQUEST_TIMEOUT,
                    heartbeat=30,
                    compress=15,
                )
            except aiohttp.WSServerHandshakeError as err:
                if err.status == 429:
                    _LOGGER.error("OBI live WebSocket was rate limited (HTTP 429)")
                    raise ObiRateLimitError(
                        "Live WebSocket was rate limited (HTTP 429)"
                    ) from err
                if err.status == 401 and attempt == 0:
                    _LOGGER.debug(
                        "OBI live WebSocket returned 401, refreshing token and retrying"
                    )
                    await self._async_relogin()
                    headers["Authorization"] = f"Bearer {self._token}"
                    continue
                if err.status in (401, 403):
                    _LOGGER.error(
                        "OBI live WebSocket authorization failed with HTTP %s",
                        err.status,
                    )
                    raise ObiAuthError(
                        f"Live WebSocket authorization failed with HTTP {err.status}"
                    ) from err
                _LOGGER.error(
                    "OBI live WebSocket handshake failed with HTTP %s",
                    err.status,
                )
                raise ObiConnectionError(
                    f"Live WebSocket handshake failed with HTTP {err.status}"
                ) from err
            except aiohttp.ClientConnectorDNSError as err:
                _LOGGER.error("DNS resolution failed for OBI live WebSocket: %s", err)
                raise ObiConnectionError(
                    "DNS resolution failed for the OBI live WebSocket endpoint"
                ) from err
            except aiohttp.ClientSSLError as err:
                _LOGGER.error("SSL/TLS error on OBI live WebSocket: %s", err)
                raise ObiConnectionError(
                    "SSL/TLS error while connecting to the OBI live WebSocket endpoint"
                ) from err
            except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as err:
                _LOGGER.error("Timeout connecting to OBI live WebSocket: %s", err)
                raise ObiConnectionError(
                    "Timeout while connecting to the OBI live WebSocket endpoint"
                ) from err
            except aiohttp.ClientConnectorError as err:
                _LOGGER.error("Could not connect to OBI live WebSocket: %s", err)
                raise ObiConnectionError(
                    "Could not connect to the OBI live WebSocket endpoint"
                ) from err
            except aiohttp.ClientError as err:
                _LOGGER.error("Network error on OBI live WebSocket: %s", err)
                raise ObiConnectionError("Network error during OBI live WebSocket") from err

        raise ObiAuthError("Live WebSocket was not authorized after refreshing token")

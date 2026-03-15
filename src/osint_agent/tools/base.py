"""Base class for OSINT tool adapters."""

import abc
import asyncio
import json
import logging
import subprocess
from pathlib import Path

import httpx

from osint_agent.models import ErrorCategory, Finding, ToolError

logger = logging.getLogger(__name__)

# HTTP status codes that warrant a retry (transient server/rate-limit errors)
_RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})


async def retry_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = 3,
    initial_backoff: float = 2.0,
    retryable_codes: frozenset[int] = _RETRYABLE_CODES,
    **kwargs,
) -> httpx.Response:
    """Make an HTTP request with automatic retry on transient failures.

    Retries on connection errors and responses with status codes in
    retryable_codes. Uses exponential backoff with jitter.

    Args:
        client: httpx.AsyncClient to use.
        method: HTTP method (GET, POST, etc.).
        url: Request URL.
        max_retries: Maximum number of retries (0 = no retries).
        initial_backoff: Seconds to wait before first retry.
        retryable_codes: HTTP status codes that trigger a retry.
        **kwargs: Passed through to client.request().

    Returns:
        httpx.Response (caller should still check .raise_for_status()).

    Raises:
        httpx.HTTPError: If all retries are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code not in retryable_codes or attempt == max_retries:
                return resp
            # Retryable status code — back off and try again
            backoff = initial_backoff * (2 ** attempt)
            # Respect Retry-After header if present (common for 429)
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    backoff = max(backoff, float(retry_after))
                except ValueError:
                    pass
            logger.info(
                "Retry %d/%d for %s %s (HTTP %d, backoff %.1fs)",
                attempt + 1, max_retries, method, url,
                resp.status_code, backoff,
            )
            await asyncio.sleep(backoff)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            last_exc = exc
            if attempt == max_retries:
                raise
            backoff = initial_backoff * (2 ** attempt)
            logger.info(
                "Retry %d/%d for %s %s (%s, backoff %.1fs)",
                attempt + 1, max_retries, method, url,
                type(exc).__name__, backoff,
            )
            await asyncio.sleep(backoff)

    # Should not reach here, but just in case
    if last_exc:
        raise last_exc
    raise httpx.HTTPError(f"Retries exhausted for {method} {url}")


class ToolAdapter(abc.ABC):
    """Base class that all tool wrappers implement.

    Each adapter is responsible for:
    1. Checking if the tool is installed/available
    2. Running the tool with appropriate arguments
    3. Parsing output into Finding objects
    """

    name: str = "base"

    @abc.abstractmethod
    async def run(self, **kwargs) -> Finding:
        """Execute the tool and return normalized findings."""

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this tool is installed and configured."""

    async def safe_run(self, **kwargs) -> Finding:
        """Execute run() with automatic error recovery.

        Catches unhandled exceptions from adapters and returns a Finding
        with a structured ToolError instead of raising. Adapters that
        handle their own errors return normally — this only catches
        what leaks through.
        """
        try:
            return await self.run(**kwargs)
        except httpx.HTTPStatusError as exc:
            error = ToolError.for_http_status(
                tool=self.name,
                status=exc.response.status_code,
                headers=dict(exc.response.headers),
            )
            return Finding(
                notes=f"{self.name}: HTTP {exc.response.status_code}",
                error=error,
            )
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
            return Finding(
                notes=f"{self.name}: network error ({type(exc).__name__})",
                error=ToolError(
                    tool=self.name,
                    category=ErrorCategory.NETWORK,
                    message=str(exc),
                    suggestion="Check network connectivity or increase timeout",
                ),
            )
        except asyncio.TimeoutError:
            return Finding(
                notes=f"{self.name}: operation timed out",
                error=ToolError(
                    tool=self.name,
                    category=ErrorCategory.TIMEOUT,
                    message="Operation timed out",
                    suggestion="Try again or increase timeout setting",
                ),
            )
        except Exception as exc:
            return Finding(
                notes=f"{self.name}: {type(exc).__name__}: {exc}",
                error=ToolError(
                    tool=self.name,
                    category=ErrorCategory.UNKNOWN,
                    message=f"{type(exc).__name__}: {exc}",
                    suggestion="Check logs for details",
                ),
            )

    async def run_subprocess(
        self,
        cmd: list[str],
        timeout: int = 120,
    ) -> subprocess.CompletedProcess:
        """Run an external CLI tool as a subprocess."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=proc.returncode,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )

    def parse_json_output(self, raw: str) -> dict | list:
        """Parse JSON from tool output, handling common issues."""
        raw = raw.strip()
        if not raw:
            return {}
        # Some tools emit multiple JSON objects (one per line)
        if raw.startswith("[") or raw.startswith("{"):
            return json.loads(raw)
        # NDJSON: one JSON object per line
        lines = [line for line in raw.splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

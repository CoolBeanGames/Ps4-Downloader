"""Archive.org search and metadata access for PKG_D0Wn.

The GUI deliberately depends on this module instead of making requests itself.  That
keeps network and pagination behavior deterministic and makes it possible to verify
that a search really visits every result and every metadata file entry.
"""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
import ipaddress
import logging
import os
import threading
import time
from typing import Callable, Iterator
from urllib.parse import quote, urlsplit

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool
from urllib3.util import Retry, Timeout


SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{name}"


class ArchiveSearchError(RuntimeError):
    """An Archive.org response could not be retrieved or understood."""


class SearchCancelled(ArchiveSearchError):
    """The caller cancelled an in-progress search."""


@dataclass(frozen=True, slots=True)
class ArchiveFile:
    """One file entry from one Archive.org item's metadata."""

    identifier: str
    name: str
    size: int | None
    mtime: str
    download_url: str


@dataclass(slots=True)
class SearchStats:
    """Mutable progress counters for a complete search."""

    total_items: int = 0
    processed_items: int = 0
    files_found: int = 0
    failed_items: int = 0
    pages_fetched: int = 0

    def snapshot(self) -> "SearchStats":
        return SearchStats(
            total_items=self.total_items,
            processed_items=self.processed_items,
            files_found=self.files_found,
            failed_items=self.failed_items,
            pages_fetched=self.pages_fetched,
        )


ProgressCallback = Callable[[SearchStats], None]
WarningCallback = Callable[[str, str], None]


def _is_name_resolution_error(error: BaseException) -> bool:
    """Recognize the wrapped urllib3/requests forms of a DNS lookup failure."""

    seen: set[int] = set()
    pending: list[BaseException | None] = [error]
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        message = str(current).lower()
        if (
            "name resolution" in message
            or "getaddrinfo failed" in message
            or "failed to resolve" in message
        ):
            return True
        pending.append(getattr(current, "__cause__", None))
        pending.append(getattr(current, "__context__", None))
    return False


class DNSOverHTTPSResolver:
    """Resolve Archive.org hosts when the operating system DNS service is broken.

    Only the hostname is sent to a DNS provider; search terms and file paths never
    leave the normal Archive.org TLS connection. Results are validated as IPv4
    addresses and cached no longer than the returned DNS TTL.
    """

    ENDPOINTS = (
        "https://1.1.1.1/dns-query",  # Cloudflare; its certificate covers this IP.
        "https://8.8.8.8/resolve",  # Google; independent fallback.
    )

    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._failed_system_hosts: set[str] = set()
        self._lock = threading.Lock()

    def system_dns_failed(self, hostname: str) -> bool:
        with self._lock:
            return hostname in self._failed_system_hosts

    def mark_system_dns_failed(self, hostname: str) -> bool:
        """Remember a broken system resolver; return True only on first report."""

        with self._lock:
            first_report = hostname not in self._failed_system_hosts
            self._failed_system_hosts.add(hostname)
            return first_report

    def resolve(self, hostname: str) -> tuple[str, ...]:
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(hostname)
            if cached and cached[0] > now:
                return cached[1]

        errors = []
        for endpoint in self.ENDPOINTS:
            session = requests.Session()
            # A proxy hostname would require the same broken system resolver. Direct
            # IP endpoints are specifically used so this recovery path needs no DNS.
            session.trust_env = False
            try:
                response = session.get(
                    endpoint,
                    params={"name": hostname, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                    timeout=(5, 12),
                )
                response.raise_for_status()
                payload = response.json()
                answers = payload.get("Answer", []) if isinstance(payload, dict) else []
                addresses: list[str] = []
                ttl_values: list[int] = []
                for answer in answers if isinstance(answers, list) else []:
                    if not isinstance(answer, dict) or answer.get("type") != 1:
                        continue
                    address = str(answer.get("data", ""))
                    try:
                        parsed = ipaddress.ip_address(address)
                    except ValueError:
                        continue
                    if parsed.version == 4 and address not in addresses:
                        addresses.append(address)
                        try:
                            ttl_values.append(max(1, int(answer.get("TTL", 60))))
                        except (TypeError, ValueError):
                            ttl_values.append(60)
                if not addresses:
                    raise ValueError("DNS response contained no IPv4 addresses")
                ttl = min(min(ttl_values or [60]), 3600)
                result = tuple(addresses)
                with self._lock:
                    self._cache[hostname] = (time.monotonic() + ttl, result)
                return result
            except Exception as exc:
                errors.append(f"{urlsplit(endpoint).hostname}: {exc}")
            finally:
                session.close()
        raise requests.ConnectionError(
            "Secure DNS-over-HTTPS lookup failed (" + "; ".join(errors) + ")"
        )


_DOH_RESOLVER = DNSOverHTTPSResolver()


class ArchiveDNSFallbackAdapter(HTTPAdapter):
    """Requests adapter that bypasses broken OS DNS without weakening TLS."""

    def __init__(self, *args, resolver: DNSOverHTTPSResolver | None = None, **kwargs):
        self.resolver = resolver or _DOH_RESOLVER
        self._fallback_pools: dict[tuple, HTTPSConnectionPool] = {}
        self._fallback_pools_lock = threading.Lock()
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        super().close()
        with self._fallback_pools_lock:
            pools = list(self._fallback_pools.values())
            self._fallback_pools.clear()
        for pool in pools:
            pool.close()

    @staticmethod
    def _is_archive_host(hostname: str | None) -> bool:
        return bool(
            hostname
            and (hostname == "archive.org" or hostname.endswith(".archive.org"))
        )

    @staticmethod
    def _timeout(value) -> Timeout:
        if isinstance(value, Timeout):
            return value
        if isinstance(value, tuple):
            return Timeout(connect=value[0], read=value[1])
        if value is None:
            return Timeout(connect=None, read=None)
        return Timeout(connect=value, read=value)

    def send(
        self,
        request,
        stream=False,
        timeout=None,
        verify=True,
        cert=None,
        proxies=None,
    ):
        hostname = urlsplit(request.url).hostname
        if (
            self._is_archive_host(hostname)
            and hostname is not None
            and self.resolver.system_dns_failed(hostname)
        ):
            return self._send_via_resolved_address(
                request,
                timeout=timeout,
                verify=verify,
            )
        try:
            return super().send(
                request,
                stream=stream,
                timeout=timeout,
                verify=verify,
                cert=cert,
                proxies=proxies,
            )
        except requests.exceptions.ConnectionError as exc:
            if not self._is_archive_host(hostname) or not _is_name_resolution_error(exc):
                raise
            if self.resolver.mark_system_dns_failed(hostname):
                logging.getLogger(__name__).warning(
                    "System DNS could not resolve %s; using secure "
                    "DNS-over-HTTPS fallback for subsequent requests.",
                    hostname,
                )
            return self._send_via_resolved_address(
                request,
                timeout=timeout,
                verify=verify,
            )

    def _send_via_resolved_address(self, request, *, timeout, verify):
        parsed = urlsplit(request.url)
        hostname = parsed.hostname
        if not hostname:
            raise requests.ConnectionError("Request URL has no hostname")
        addresses = self.resolver.resolve(hostname)
        request_path = parsed.path or "/"
        if parsed.query:
            request_path += "?" + parsed.query

        headers = dict(request.headers)
        headers["Host"] = parsed.netloc
        ca_certs = certifi.where()
        ca_cert_dir = None
        if isinstance(verify, str):
            if os.path.isdir(verify):
                ca_cert_dir = verify
                ca_certs = None
            else:
                ca_certs = verify

        retry = Retry(
            total=4,
            connect=4,
            read=4,
            status=4,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(("GET",)),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        errors = []
        for address in addresses:
            pool_key = (
                hostname,
                address,
                parsed.port or 443,
                ca_certs,
                ca_cert_dir,
            )
            with self._fallback_pools_lock:
                pool = self._fallback_pools.get(pool_key)
                if pool is None:
                    pool = HTTPSConnectionPool(
                        address,
                        port=parsed.port or 443,
                        maxsize=self._pool_maxsize,
                        block=self._pool_block,
                        cert_reqs="CERT_REQUIRED",
                        ca_certs=ca_certs,
                        ca_cert_dir=ca_cert_dir,
                        assert_hostname=hostname,
                        server_hostname=hostname,
                    )
                    self._fallback_pools[pool_key] = pool
            try:
                raw_response = pool.urlopen(
                    method=request.method,
                    url=request_path,
                    body=request.body,
                    headers=headers,
                    retries=retry,
                    redirect=False,
                    assert_same_host=False,
                    preload_content=False,
                    decode_content=False,
                    timeout=self._timeout(timeout),
                )
                return self.build_response(request, raw_response)
            except Exception as exc:
                errors.append(f"{address}: {exc}")
                with self._fallback_pools_lock:
                    if self._fallback_pools.get(pool_key) is pool:
                        self._fallback_pools.pop(pool_key, None)
                pool.close()
        raise requests.ConnectionError(
            f"Archive.org was resolved securely, but HTTPS failed for {hostname} "
            f"({'; '.join(errors)})"
        )


def create_retry_session() -> requests.Session:
    """Create a session that tolerates transient DNS, connection, and HTTP errors."""

    retry = Retry(
        total=5,
        # A DNS failure is handled immediately by ArchiveDNSFallbackAdapter. Other
        # retry categories retain normal backoff behavior.
        connect=0,
        read=5,
        status=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(("GET",)),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = ArchiveDNSFallbackAdapter(
        max_retries=retry,
        pool_connections=16,
        pool_maxsize=16,
    )
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "PKG_D0Wn/2.0 (Archive.org file browser)",
        }
    )
    session.mount("https://", adapter)
    session.mount("http://", HTTPAdapter(max_retries=retry))
    return session


def build_download_url(identifier: str, name: str) -> str:
    """Build a correctly escaped Archive.org download URL."""

    encoded_identifier = quote(identifier, safe="")
    # Archive metadata names can contain subdirectories. Preserve their separators
    # while escaping spaces, #, ?, Unicode, and other URL-sensitive characters.
    encoded_name = quote(name, safe="/")
    return DOWNLOAD_URL.format(identifier=encoded_identifier, name=encoded_name)


def normalize_extension(extension: str) -> str:
    extension = extension.strip().lower()
    if not extension or extension == "*":
        return ""
    return extension if extension.startswith(".") else f".{extension}"


class ArchiveOrgClient:
    """Enumerate every file belonging to every item returned by a search."""

    def __init__(
        self,
        *,
        page_size: int = 1000,
        max_workers: int = 8,
        timeout: tuple[float, float] = (12.0, 90.0),
        session_factory: Callable[[], requests.Session] = create_retry_session,
        logger: logging.Logger | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.page_size = page_size
        self.max_workers = max_workers
        self.timeout = timeout
        self.session_factory = session_factory
        self.logger = logger or logging.getLogger(__name__)
        self._thread_local = threading.local()

    def _session(self) -> requests.Session:
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = self.session_factory()
            self._thread_local.session = session
        return session

    def _get_json(self, url: str, *, params=None, purpose: str) -> dict:
        try:
            response = self._session().get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.ConnectionError as exc:
            raise ArchiveSearchError(
                f"Could not connect to Archive.org while {purpose}. "
                "Check the internet connection, DNS, firewall, or VPN and try again."
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise ArchiveSearchError(
                f"Archive.org timed out while {purpose}. Please try again."
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise ArchiveSearchError(
                f"Archive.org request failed while {purpose}: {exc}"
            ) from exc
        except ValueError as exc:
            raise ArchiveSearchError(
                f"Archive.org returned invalid JSON while {purpose}."
            ) from exc

        if not isinstance(data, dict):
            raise ArchiveSearchError(
                f"Archive.org returned an unexpected response while {purpose}."
            )
        return data

    def _search_page(
        self,
        query: str,
        page: int,
        cursor: str | None,
    ) -> tuple[list[str], int, str | None]:
        # A stable identifier sort prevents records from moving between numbered
        # pages during a long search. Supplying params lets requests encode the
        # user's query instead of interpolating it into a URL.
        params: list[tuple[str, str]] = [
            ("q", query),
            ("fl[]", "identifier"),
            ("sort[]", "identifier asc"),
            ("rows", str(self.page_size)),
            ("output", "json"),
        ]
        if cursor is not None:
            params.append(("cursorMark", cursor))
        else:
            params.append(("page", str(page)))
        data = self._get_json(
            SEARCH_URL,
            params=params,
            purpose=f"fetching search page {page}",
        )
        response = data.get("response")
        if not isinstance(response, dict):
            # Archive.org deployments do not all enable Solr cursorMark. Some
            # return HTTP 200 plus {"error": ...} for the initial "*" token.
            # Transparently retry page 1 without a cursor and continue through
            # numbered pages so a deployment detail never becomes an empty list.
            if cursor is not None and data.get("error"):
                self.logger.warning(
                    "Archive.org rejected cursor pagination (%s); "
                    "falling back to numbered pages.",
                    data.get("error"),
                )
                return self._search_page(query, page, None)
            raise ArchiveSearchError("Archive.org search response has no result object.")
        docs = response.get("docs", [])
        if not isinstance(docs, list):
            raise ArchiveSearchError("Archive.org search response has an invalid item list.")
        try:
            total = max(0, int(response.get("numFound", len(docs))))
        except (TypeError, ValueError):
            total = len(docs)

        identifiers: list[str] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            identifier = doc.get("identifier")
            if identifier is not None and str(identifier).strip():
                identifiers.append(str(identifier))
        raw_next_cursor = data.get("nextCursorMark")
        next_cursor = (
            str(raw_next_cursor)
            if raw_next_cursor is not None and str(raw_next_cursor)
            else None
        )
        return identifiers, total, next_cursor

    def _metadata_files(self, identifier: str) -> list[dict]:
        url = METADATA_URL.format(identifier=quote(identifier, safe=""))
        data = self._get_json(
            url,
            purpose=f"fetching metadata for {identifier}",
        )
        files = data.get("files", [])
        if not isinstance(files, list):
            raise ArchiveSearchError(
                f"Archive.org returned an invalid file list for {identifier}."
            )
        return [entry for entry in files if isinstance(entry, dict)]

    @staticmethod
    def _to_archive_file(identifier: str, entry: dict) -> ArchiveFile | None:
        raw_name = entry.get("name")
        if raw_name is None or not str(raw_name):
            return None
        name = str(raw_name)
        raw_size = entry.get("size")
        try:
            size = max(0, int(raw_size)) if raw_size not in (None, "") else None
        except (TypeError, ValueError):
            size = None
        raw_mtime = entry.get("mtime", "")
        mtime = "" if raw_mtime is None else str(raw_mtime)
        return ArchiveFile(
            identifier=identifier,
            name=name,
            size=size,
            mtime=mtime,
            download_url=build_download_url(identifier, name),
        )

    def iter_files(
        self,
        query: str,
        *,
        extension: str = "",
        cancel_event: threading.Event | None = None,
        progress: ProgressCallback | None = None,
        warning: WarningCallback | None = None,
        stats: SearchStats | None = None,
    ) -> Iterator[ArchiveFile]:
        """Yield all matching files across all pages and all item metadata.

        Metadata failures are counted and reported through ``warning`` without
        hiding successfully retrieved items. Search-page failures abort because
        continuing would incorrectly present a partial list as complete.
        """

        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        extension = normalize_extension(extension)
        cancel_event = cancel_event or threading.Event()
        stats = stats or SearchStats()
        seen_identifiers: set[str] = set()
        page = 1
        cursor: str | None = "*"

        while True:
            if cancel_event.is_set():
                raise SearchCancelled("Search cancelled.")

            identifiers, reported_total, next_cursor = self._search_page(
                query,
                page,
                cursor,
            )
            stats.pages_fetched += 1
            stats.total_items = max(stats.total_items, reported_total)
            new_identifiers: list[str] = []
            page_identifiers: set[str] = set()
            for identifier in identifiers:
                if identifier in seen_identifiers or identifier in page_identifiers:
                    continue
                page_identifiers.add(identifier)
                new_identifiers.append(identifier)
            if identifiers and not new_identifiers:
                raise ArchiveSearchError(
                    "Archive.org search pagination stopped making progress; "
                    "the complete result set could not be guaranteed."
                )
            seen_identifiers.update(new_identifiers)
            if progress:
                progress(stats.snapshot())

            executor = ThreadPoolExecutor(
                max_workers=self.max_workers,
                thread_name_prefix="archive-metadata",
            )
            futures = {
                executor.submit(self._metadata_files, identifier): identifier
                for identifier in new_identifiers
            }
            cancelled = False
            try:
                pending = set(futures)
                while pending:
                    if cancel_event.is_set():
                        cancelled = True
                        for future in pending:
                            future.cancel()
                        raise SearchCancelled("Search cancelled.")
                    completed, pending = wait(
                        pending,
                        timeout=0.2,
                        return_when=FIRST_COMPLETED,
                    )
                    for future in completed:
                        identifier = futures[future]
                        try:
                            entries = future.result()
                        except ArchiveSearchError as exc:
                            stats.failed_items += 1
                            self.logger.error("Metadata error for %s: %s", identifier, exc)
                            if warning:
                                warning(identifier, str(exc))
                        except Exception as exc:  # defensive: keep one item from killing a search
                            stats.failed_items += 1
                            self.logger.exception("Unexpected metadata error for %s", identifier)
                            if warning:
                                warning(identifier, str(exc))
                        else:
                            for entry in entries:
                                archive_file = self._to_archive_file(identifier, entry)
                                if archive_file is None:
                                    continue
                                if extension and not archive_file.name.lower().endswith(extension):
                                    continue
                                stats.files_found += 1
                                yield archive_file
                        finally:
                            stats.processed_items += 1
                            if progress:
                                progress(stats.snapshot())
            finally:
                executor.shutdown(wait=not cancelled, cancel_futures=cancelled)

            # Cursor marks avoid the deep-paging limit for large result sets. If an
            # older endpoint omits nextCursorMark, continue with numbered pages.
            if not identifiers:
                break
            if len(seen_identifiers) >= stats.total_items:
                break
            if cursor is not None and next_cursor is not None:
                if next_cursor == cursor:
                    break
                cursor = next_cursor
            else:
                if cursor is not None:
                    cursor = None
                    page = 2
                else:
                    page += 1
                if len(identifiers) < self.page_size:
                    break

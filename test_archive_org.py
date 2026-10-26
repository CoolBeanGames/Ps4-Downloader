import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

import requests

from archive_org import (
    ArchiveDNSFallbackAdapter,
    ArchiveOrgClient,
    ArchiveSearchError,
    DNSOverHTTPSResolver,
    SearchCancelled,
    SearchStats,
    build_download_url,
)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(f"HTTP {self.status_code}", response=response)

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, search_pages, metadata, *, connection_error=False):
        self.search_pages = search_pages
        self.metadata = metadata
        self.connection_error = connection_error
        self.search_calls = []
        self.metadata_calls = []

    def get(self, url, params=None, timeout=None):
        if self.connection_error:
            raise requests.ConnectionError("DNS lookup failed")
        if url.endswith("advancedsearch.php"):
            params_dict = dict(params)
            self.search_calls.append(params_dict)
            key = params_dict.get("cursorMark")
            if key is None:
                key = int(params_dict["page"])
            return FakeResponse(self.search_pages[key])
        identifier = unquote(url.rsplit("/", 1)[-1])
        self.metadata_calls.append(identifier)
        response = self.metadata[identifier]
        return response if isinstance(response, FakeResponse) else FakeResponse(response)


def make_client(session, page_size=2):
    return ArchiveOrgClient(
        page_size=page_size,
        max_workers=2,
        session_factory=lambda: session,
    )


class ArchiveOrgClientTests(unittest.TestCase):
    def test_all_pages_and_every_metadata_file_are_enumerated(self):
        pages = {
            "*": {
                "response": {
                    "numFound": 3,
                    "docs": [{"identifier": "item one"}, {"identifier": "item-2"}],
                },
                "nextCursorMark": "cursor-2",
            },
            "cursor-2": {
                "response": {
                    "numFound": 3,
                    "docs": [{"identifier": "item-3"}],
                },
                "nextCursorMark": "cursor-3",
            },
        }
        metadata = {
            "item one": {
                "files": [
                    {"name": "game.pkg", "size": "12", "mtime": "100"},
                    {"name": "game_archive.torrent", "size": "4"},
                    {"name": "folder/cover #1.jpg"},
                ]
            },
            "item-2": {"files": [{"name": "notes.txt", "size": "3"}]},
            "item-3": {"files": [{"name": "second.pkg", "size": "8"}]},
        }
        session = FakeSession(pages, metadata)
        stats = SearchStats()

        files = list(make_client(session).iter_files("ps4 pkg", stats=stats))

        self.assertCountEqual(
            [entry.name for entry in files],
            ["game.pkg", "game_archive.torrent", "folder/cover #1.jpg", "notes.txt", "second.pkg"],
        )
        self.assertEqual(stats.total_items, 3)
        self.assertEqual(stats.processed_items, 3)
        self.assertEqual(stats.files_found, 5)
        self.assertEqual(stats.pages_fetched, 2)
        self.assertEqual(
            [call["cursorMark"] for call in session.search_calls],
            ["*", "cursor-2"],
        )
        self.assertTrue(all(call["q"] == "ps4 pkg" for call in session.search_calls))
        self.assertTrue(all(call["sort[]"] == "identifier asc" for call in session.search_calls))

    def test_extension_filter_is_optional_and_case_insensitive(self):
        pages = {"*": {"response": {"numFound": 1, "docs": [{"identifier": "x"}]}}}
        metadata = {
            "x": {
                "files": [
                    {"name": "UPPER.PKG"},
                    {"name": "readme.txt"},
                    {"name": "also.pkg"},
                ]
            }
        }

        files = list(make_client(FakeSession(pages, metadata)).iter_files("x", extension="pkg"))

        self.assertCountEqual([entry.name for entry in files], ["UPPER.PKG", "also.pkg"])

    def test_download_urls_escape_identifiers_and_names(self):
        url = build_download_url("item one", "folder/cover #1?.jpg")
        self.assertEqual(
            url,
            "https://archive.org/download/item%20one/folder/cover%20%231%3F.jpg",
        )

    def test_metadata_failure_is_reported_without_hiding_other_items(self):
        pages = {
            "*": {
                "response": {
                    "numFound": 2,
                    "docs": [{"identifier": "good"}, {"identifier": "bad"}],
                }
            }
        }
        metadata = {
            "good": {"files": [{"name": "good.bin"}]},
            "bad": FakeResponse({}, status=503),
        }
        warnings = []
        stats = SearchStats()

        files = list(
            make_client(FakeSession(pages, metadata)).iter_files(
                "anything",
                warning=lambda identifier, message: warnings.append((identifier, message)),
                stats=stats,
            )
        )

        self.assertEqual([entry.name for entry in files], ["good.bin"])
        self.assertEqual(stats.failed_items, 1)
        self.assertEqual(warnings[0][0], "bad")

    def test_numbered_page_fallback_when_cursor_is_not_returned(self):
        pages = {
            "*": {
                "response": {
                    "numFound": 3,
                    "docs": [{"identifier": "one"}, {"identifier": "two"}],
                }
            },
            2: {
                "response": {
                    "numFound": 3,
                    "docs": [{"identifier": "three"}],
                }
            },
        }
        metadata = {
            "one": {"files": [{"name": "one.bin"}]},
            "two": {"files": [{"name": "two.bin"}]},
            "three": {"files": [{"name": "three.bin"}]},
        }
        session = FakeSession(pages, metadata)

        files = list(make_client(session).iter_files("fallback"))

        self.assertCountEqual(
            [entry.name for entry in files],
            ["one.bin", "two.bin", "three.bin"],
        )
        self.assertEqual(session.search_calls[0]["cursorMark"], "*")
        self.assertEqual(session.search_calls[1]["page"], "2")

    def test_numbered_page_fallback_when_archive_rejects_cursor(self):
        pages = {
            "*": {"error": "cursorMark is unsupported"},
            1: {
                "response": {
                    "numFound": 3,
                    "docs": [{"identifier": "one"}, {"identifier": "two"}],
                }
            },
            2: {
                "response": {
                    "numFound": 3,
                    "docs": [{"identifier": "three"}],
                }
            },
        }
        metadata = {
            "one": {"files": [{"name": "one.bin"}]},
            "two": {"files": [{"name": "two.bin"}]},
            "three": {"files": [{"name": "three.bin"}]},
        }
        session = FakeSession(pages, metadata)

        files = list(make_client(session).iter_files("fallback"))

        self.assertCountEqual(
            [entry.name for entry in files],
            ["one.bin", "two.bin", "three.bin"],
        )
        self.assertEqual(session.search_calls[0]["cursorMark"], "*")
        self.assertEqual(session.search_calls[1]["page"], "1")
        self.assertEqual(session.search_calls[2]["page"], "2")

    def test_connection_errors_become_useful_archive_errors(self):
        session = FakeSession({}, {}, connection_error=True)
        with self.assertRaisesRegex(ArchiveSearchError, "DNS"):
            list(make_client(session).iter_files("test"))

    def test_pre_cancelled_search_makes_no_request(self):
        session = FakeSession({}, {})
        cancelled = threading.Event()
        cancelled.set()

        with self.assertRaises(SearchCancelled):
            list(make_client(session).iter_files("test", cancel_event=cancelled))
        self.assertEqual(session.search_calls, [])

    def test_archive_dns_failure_uses_secure_fallback(self):
        resolver = DNSOverHTTPSResolver()
        adapter = ArchiveDNSFallbackAdapter(max_retries=0, resolver=resolver)
        prepared = requests.Request(
            "GET",
            "https://archive.org/advancedsearch.php?q=test",
        ).prepare()
        expected = Mock()

        with patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            side_effect=requests.ConnectionError("getaddrinfo failed"),
        ), patch.object(
            adapter,
            "_send_via_resolved_address",
            return_value=expected,
        ) as fallback:
            response = adapter.send(prepared, timeout=(1, 2))

        self.assertIs(response, expected)
        fallback.assert_called_once_with(prepared, timeout=(1, 2), verify=True)
        self.assertTrue(resolver.system_dns_failed("archive.org"))

    def test_non_archive_dns_failure_is_not_redirected(self):
        adapter = ArchiveDNSFallbackAdapter(max_retries=0)
        prepared = requests.Request("GET", "https://example.com/").prepare()

        with patch.object(
            requests.adapters.HTTPAdapter,
            "send",
            side_effect=requests.ConnectionError("getaddrinfo failed"),
        ), self.assertRaises(requests.ConnectionError):
            adapter.send(prepared)


if __name__ == "__main__":
    unittest.main()

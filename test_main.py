import unittest
from pathlib import Path
import queue
import tempfile
import threading
from unittest.mock import patch

import main as main_module
from archive_org import ArchiveFile
from main import (
    DownloadTask,
    PKGDownApp,
    archive_file_basename,
    calculate_eta,
    file_destination,
    format_eta,
    load_app_settings,
    local_file_is_complete,
    local_file_status,
    resolve_download_directory,
    result_matches_filters,
    result_sort_value,
    selected_size_totals,
    storage_volume_info,
    write_app_settings,
)


def sample(name, size, mtime=""):
    return ArchiveFile(
        identifier="item",
        name=name,
        size=size,
        mtime=mtime,
        download_url=f"https://archive.org/download/item/{name}",
    )


class ResultBehaviorTests(unittest.TestCase):
    def test_size_sort_value_is_numeric_and_unknown_is_marked(self):
        small = result_sort_value(sample("small.pkg", 9), "size")
        large = result_sort_value(sample("large.pkg", 100), "size")
        unknown = result_sort_value(sample("unknown.pkg", None), "size")

        self.assertLess(small, large)
        self.assertTrue(unknown[0])

    def test_modified_sort_value_handles_epoch_and_iso_dates(self):
        epoch = result_sort_value(sample("a", 1, "100"), "modified")
        iso = result_sort_value(
            sample("b", 1, "2026-09-24T02:00:00+00:00"),
            "modified",
        )

        self.assertEqual(epoch, (False, 0, 100.0))
        self.assertFalse(iso[0])
        self.assertIsInstance(iso[2], float)

    def test_text_sort_is_case_insensitive(self):
        upper = result_sort_value(sample("ALPHA.pkg", 1), "file")
        lower = result_sort_value(sample("beta.pkg", 1), "file")
        self.assertLess(upper, lower)

    def test_sink_scroll_constants_are_preserved(self):
        self.assertEqual(PKGDownApp.PAN_MULTIPLIER, 1.6)
        self.assertEqual(PKGDownApp.EDGE_SCROLL_ZONE, 150)
        self.assertEqual(PKGDownApp.EDGE_SCROLL_STEP, 14)
        self.assertEqual(PKGDownApp.EDGE_SCROLL_INTERVAL_MS, 30)
        self.assertEqual(PKGDownApp.WHEEL_SCROLL_STEP, 144)

    def test_eta_formatting_and_calculation(self):
        self.assertEqual(format_eta(None), "\u2014")
        self.assertEqual(format_eta(0), "<1s")
        self.assertEqual(format_eta(65), "1:05")
        self.assertEqual(format_eta(3661), "1:01:01")
        self.assertEqual(format_eta(90061), "1d 01:01:01")
        self.assertEqual(calculate_eta(1024, 1), 1.0)
        self.assertIsNone(calculate_eta(1024, 0))

    def test_selected_size_totals_keep_unknown_count_honest(self):
        known_bytes, unknown_count = selected_size_totals(
            [sample("one.pkg", 100), sample("two.pkg", None), sample("three.pkg", 25)]
        )
        self.assertEqual(known_bytes, 125)
        self.assertEqual(unknown_count, 1)

    def test_local_file_status_only_marks_matching_size_as_fully_downloaded(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "game.pkg"
            archive_file = sample("game.pkg", 4)

            self.assertEqual(local_file_status(destination, archive_file), "Ready")
            destination.write_bytes(b"123")
            self.assertEqual(local_file_status(destination, archive_file), "\u26a0 Incomplete")
            self.assertFalse(local_file_is_complete(destination, archive_file))
            destination.write_bytes(b"1234")
            self.assertEqual(local_file_status(destination, archive_file), "\u2713 Downloaded")
            self.assertTrue(local_file_is_complete(destination, archive_file))

    def test_destination_is_flattened_to_a_safe_basename(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = file_destination(
                temporary_directory,
                sample("source/page/files/My Game.pkg", 4),
            )
            self.assertEqual(destination.parent, Path(temporary_directory).resolve())
            self.assertEqual(destination.name, "My Game.pkg")

            reserved = file_destination(
                temporary_directory,
                sample("somewhere/CON.pkg", 4),
            )
            self.assertEqual(reserved.name, "_CON.pkg")

    def test_filename_filter_matches_only_basename_case_insensitively(self):
        archive_file = sample("folder-that-must-not-match/My Cool Game.PKG", 4)
        self.assertEqual(archive_file_basename(archive_file), "My Cool Game.PKG")
        self.assertTrue(
            result_matches_filters(
                archive_file,
                filename_filter="cool game",
                hide_downloaded=False,
                is_downloaded=False,
            )
        )
        self.assertFalse(
            result_matches_filters(
                archive_file,
                filename_filter="FOLDER-THAT",
                hide_downloaded=False,
                is_downloaded=False,
            )
        )

    def test_hide_downloaded_keeps_incomplete_and_ready_records(self):
        archive_file = sample("game.pkg", 4)
        self.assertFalse(
            result_matches_filters(
                archive_file,
                filename_filter="",
                hide_downloaded=True,
                is_downloaded=True,
            )
        )
        self.assertTrue(
            result_matches_filters(
                archive_file,
                filename_filter="",
                hide_downloaded=True,
                is_downloaded=False,
            )
        )

    def test_settings_round_trip_all_persisted_fields(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings_file = root / "profile" / "settings.json"
            download_directory = root / "downloads"
            download_directory.mkdir()
            expected = {
                "version": 1,
                "search_history": ["first", "second"],
                "search_text": "ps4 collection",
                "extension_filter": "pkg",
                "download_directory": str(download_directory),
                "sort_column": "size",
                "sort_descending": True,
                "filename_filter": "game",
                "hide_downloaded": True,
            }
            write_app_settings(settings_file, expected)
            actual = load_app_settings(
                settings_file,
                legacy_file=None,
                fallback_download_dir=root / "fallback",
            )

            self.assertEqual(actual["search_history"], expected["search_history"])
            self.assertEqual(actual["search_text"], expected["search_text"])
            self.assertEqual(actual["extension_filter"], "pkg")
            self.assertEqual(
                actual["download_directory"],
                str(download_directory.resolve()),
            )
            self.assertEqual(actual["sort_column"], "size")
            self.assertTrue(actual["sort_descending"])
            self.assertEqual(actual["filename_filter"], "game")
            self.assertTrue(actual["hide_downloaded"])

    def test_unavailable_saved_directory_falls_back_to_downloads(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            fallback = root / "Downloads"
            fallback.mkdir()
            missing = root / "disconnected-drive" / "downloads"
            self.assertEqual(
                resolve_download_directory(str(missing), fallback=fallback),
                fallback.resolve(),
            )

            settings_file = root / "settings.json"
            write_app_settings(
                settings_file,
                {"download_directory": str(missing)},
            )
            loaded = load_app_settings(
                settings_file,
                legacy_file=None,
                fallback_download_dir=fallback,
            )
            self.assertEqual(loaded["download_directory"], str(fallback.resolve()))

    def test_legacy_history_list_is_migrated(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            legacy_file = root / "search_history.json"
            legacy_file.write_text('["old query"]', encoding="utf-8")
            loaded = load_app_settings(
                root / "missing-settings.json",
                legacy_file=legacy_file,
                fallback_download_dir=root,
            )
            self.assertEqual(loaded["search_history"], ["old query"])

    def test_unknown_size_existing_file_is_visibly_unverified(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "unknown.pkg"
            destination.write_bytes(b"data")
            self.assertEqual(
                local_file_status(destination, sample("unknown.pkg", None)),
                "Downloaded (unverified)",
            )

    def test_storage_info_accepts_a_not_yet_created_child_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            child = Path(temporary_directory) / "future" / "downloads"
            volume, free_bytes = storage_volume_info(str(child))
            self.assertTrue(volume)
            self.assertGreater(free_bytes, 0)

    def test_cancelled_download_removes_partial_file(self):
        chunk_ready = threading.Event()
        release_second_chunk = threading.Event()

        class FakeResponse:
            headers = {"content-length": "8"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                self.assert_chunk_size = chunk_size
                yield b"1234"
                chunk_ready.set()
                release_second_chunk.wait(2)
                yield b"5678"

        class FakeSession:
            def __init__(self):
                self.headers = {}

            def get(self, *_args, **_kwargs):
                return FakeResponse()

        app = object.__new__(PKGDownApp)
        app.download_queue = queue.Queue()
        app.ui_events = queue.Queue()
        cancel_event = threading.Event()
        with tempfile.TemporaryDirectory() as temporary_directory:
            destination = Path(temporary_directory) / "cancelled.pkg"
            task = DownloadTask(
                token=1,
                iid="result-1",
                archive_file=sample("cancelled.pkg", 8),
                destination=destination,
                size_bytes=8,
                size_known=True,
                cancel_event=cancel_event,
            )
            app.download_queue.put(task)
            with patch.object(main_module, "create_retry_session", return_value=FakeSession()):
                worker = threading.Thread(target=app.download_worker, daemon=True)
                worker.start()
                self.assertTrue(chunk_ready.wait(2))
                cancel_event.set()
                release_second_chunk.set()
                terminal_event = None
                while terminal_event is None:
                    event = app.ui_events.get(timeout=2)
                    if event[0] in ("download_done", "download_cancelled", "download_error"):
                        terminal_event = event

            self.assertEqual(terminal_event[0], "download_cancelled")
            self.assertFalse(destination.exists())
            self.assertFalse(destination.with_name("cancelled.pkg.part").exists())


if __name__ == "__main__":
    unittest.main()

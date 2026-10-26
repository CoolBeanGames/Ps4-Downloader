from __future__ import annotations

from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading

from archive_org import ArchiveFile
from main import DownloadTask, PKGDownApp, file_destination, load_app_settings


def archive_file(name: str, size: int | None) -> ArchiveFile:
    return ArchiveFile(
        identifier="smoke-item",
        name=name,
        size=size,
        mtime="",
        download_url=f"https://archive.org/download/smoke-item/{name}",
    )


def verify_reloaded_settings(settings_file: Path, download_directory: Path) -> None:
    app = PKGDownApp(settings_file=settings_file, legacy_config_file=None)
    app.withdraw()
    assert app.search_entry.get() == "saved search"
    assert app.ext_entry.get() == "pkg"
    assert app.download_dir.get() == str(download_directory.resolve())
    assert app.sort_column == "size"
    assert app.sort_descending is True
    assert "\u25bc" in app.results_tree.heading("size", "text")
    assert app.filename_filter_var.get() == "partial"
    assert app.hide_downloaded_var.get() is True
    app.on_close()


def main() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        settings_file = temporary_path / "settings" / "settings.json"
        app = PKGDownApp(settings_file=settings_file, legacy_config_file=None)
        app.withdraw()
        app.download_dir.set(temporary_directory)

        complete = archive_file("archive/page/Downloaded Game.pkg", 4)
        incomplete = archive_file("archive/page/Partial Game.pkg", 5)
        unknown = archive_file("archive/page/Other File.pkg", None)
        complete_path = file_destination(temporary_directory, complete)
        assert complete_path.parent == temporary_path.resolve(), complete_path
        complete_path.parent.mkdir(parents=True, exist_ok=True)
        complete_path.write_bytes(b"1234")
        incomplete_path = file_destination(temporary_directory, incomplete)
        incomplete_path.write_bytes(b"12")

        app.add_result_batch([complete, incomplete, unknown])
        iids = app.results_tree.get_children("")
        statuses = [app.results_tree.item(iid, "values")[4] for iid in iids]
        assert statuses == ["\u2713 Downloaded", "\u26a0 Incomplete", "Ready"], statuses

        app.results_tree.selection_set(iids[0])
        app.filename_filter_var.set("GAME")
        assert app.results_tree.get_children("") == (iids[0], iids[1])
        app.filename_filter_var.set("partial")
        assert app.results_tree.get_children("") == (iids[1],)
        assert not app.results_tree.selection(), app.results_tree.selection()
        app.filename_filter_var.set("game")
        app.hide_downloaded_var.set(True)
        app.on_result_filter_changed()
        assert app.results_tree.get_children("") == (iids[1],)
        assert app.result_count_label.cget("text").startswith("1 shown / 3 files")

        app.filename_filter_var.set("")
        assert app.results_tree.get_children("") == (iids[1], iids[2])
        unknown_path = file_destination(temporary_directory, unknown)
        unknown_path.write_bytes(b"now present")
        app.refresh_download_statuses()
        assert app.results_tree.get_children("") == (iids[1],)
        unknown_path.unlink()
        app.refresh_download_statuses()
        assert app.results_tree.get_children("") == (iids[1], iids[2])

        app.hide_downloaded_var.set(False)
        app.on_result_filter_changed()
        assert app.results_tree.get_children("") == iids

        app.results_tree.selection_set(iids[0])
        app.queue_selected_downloads()
        assert app.queued_files == 0
        assert app.results_tree.item(iids[0], "values")[4] == "\u2713 Downloaded"

        app.results_tree.selection_set(iids)
        app.update_result_count_label()
        preview = app.selected_space_label.cget("text")
        assert "Selected to download: 5 B" in preview, preview
        assert "1 unknown-size file" in preview, preview
        assert "1 already complete" in preview, preview
        assert "Free on" in preview, preview

        current_event = threading.Event()
        current_task = DownloadTask(
            token=app.search_token,
            iid=iids[1],
            archive_file=incomplete,
            destination=incomplete_path,
            size_bytes=2048,
            size_known=True,
            cancel_event=current_event,
        )
        app.current_download_task = current_task
        app.current_downloading = incomplete.name
        app.current_download_total_known = True
        app.current_download_total_bytes = 2048
        app.current_downloaded_bytes = 1024
        app.current_speed = 1.0
        app.queued_files = 2
        app.queued_bytes = 3072
        app.update_bottom_bar()
        assert app.status_current_eta_label.cget("text") == "Current ETA: 0:01"
        assert app.status_all_eta_label.cget("text") == "All ETA: 0:02"

        queued_task = DownloadTask(
            token=app.search_token,
            iid=iids[2],
            archive_file=unknown,
            destination=Path(temporary_directory) / "queued.pkg",
            size_bytes=0,
            size_known=False,
            cancel_event=current_event,
        )
        app.download_queue = queue.Queue()
        app.download_queue.put(queued_task)
        app.pending_downloads = {
            (current_task.token, current_task.iid),
            (queued_task.token, queued_task.iid),
        }
        app.queued_files = 2
        app.queued_bytes = current_task.size_bytes
        app.queued_unknown_files = 1
        app.download_cancel_event = current_event
        app.stop_downloads()
        assert current_event.is_set()
        assert app.download_queue.empty()
        assert app.queued_files == 1
        assert app.queued_unknown_files == 0
        assert not app.download_cancel_event.is_set()
        assert app.stop_downloads_btn.cget("state") == "disabled"

        app.handle_ui_event(("download_cancelled", current_task))
        assert app.queued_files == 0
        assert app.queued_bytes == 0
        assert app.stop_downloads_btn.cget("state") == "disabled"
        assert app.results_tree.item(current_task.iid, "values")[4] == "Stopped"

        app.clear_results()
        assert not app.result_records
        assert not app.hidden_result_iids
        app.add_result_batch([archive_file("fresh/result.pkg", 1)])
        assert tuple(app.result_records) == ("result-1",)
        assert app.results_tree.get_children("") == ("result-1",)

        app.search_history = ["saved history"]
        app.search_entry.delete(0, "end")
        app.search_entry.insert(0, "saved search")
        app.ext_entry.insert(0, "pkg")
        app.sort_results("size")
        app.sort_results("size")
        app.filename_filter_var.set("partial")
        app.hide_downloaded_var.set(True)
        app.on_result_filter_changed()
        app.save_settings()

        persisted = load_app_settings(
            settings_file,
            legacy_file=None,
            fallback_download_dir=temporary_path,
        )
        assert persisted["search_text"] == "saved search", persisted
        assert persisted["extension_filter"] == "pkg", persisted
        assert persisted["download_directory"] == str(temporary_path.resolve()), persisted
        assert persisted["sort_column"] == "size", persisted
        assert persisted["sort_descending"] is True, persisted
        assert persisted["search_history"] == ["saved history"], persisted
        assert persisted["filename_filter"] == "partial", persisted
        assert persisted["hide_downloaded"] is True, persisted

        app.on_close()
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--reload-check",
                str(settings_file),
                str(temporary_path),
            ],
            check=True,
        )

    print("GUI smoke test passed")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--reload-check":
        verify_reloaded_settings(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        main()

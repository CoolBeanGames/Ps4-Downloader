from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from archive_org import (
    ArchiveFile,
    ArchiveOrgClient,
    ArchiveSearchError,
    SearchCancelled,
    SearchStats,
    create_retry_session,
)


APP_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)
SETTINGS_DIR = Path(
    os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
) / "PKG_D0Wn"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"
LEGACY_CONFIG_FILE = APP_DIR / "search_history.json"
LOG_FILE = APP_DIR / "pkg_down.log"
SORT_COLUMNS = {"#0", "item", "file", "size", "modified", "status", "url"}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
LOGGER = logging.getLogger("pkg_down")
LOGGER.info("Application started.")


def format_bytes(size: int | None) -> str:
    if size is None:
        return "Unknown"
    value = float(size)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value):,} {unit}"
            return f"{value:,.1f} {unit}"
        value /= 1024
    return f"{size:,} B"


def format_mtime(value: str) -> str:
    if not value:
        return "Unknown"
    try:
        timestamp = float(value)
        return dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, OSError, OverflowError):
        return value


def format_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    rounded = int(seconds + 0.5)
    if rounded < 1:
        return "<1s"
    days, remainder = divmod(rounded, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def calculate_eta(remaining_bytes: int | float, speed_kbps: float) -> float | None:
    if remaining_bytes < 0 or speed_kbps <= 0:
        return None
    return float(remaining_bytes) / (speed_kbps * 1024)


def selected_size_totals(files: list[ArchiveFile]) -> tuple[int, int]:
    known_bytes = sum(entry.size for entry in files if entry.size is not None)
    unknown_count = sum(1 for entry in files if entry.size is None)
    return known_bytes, unknown_count


def storage_volume_info(path: str) -> tuple[str, int]:
    """Return the containing volume label and current free bytes.

    The configured download directory does not need to exist yet; walk upward to
    the nearest existing parent on the same volume before asking the OS.
    """

    candidate = Path(path).expanduser().resolve()
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    usage = shutil.disk_usage(candidate)
    volume = candidate.anchor or str(candidate)
    return volume, usage.free


def default_download_directory() -> Path:
    return Path.home() / "Downloads"


def resolve_download_directory(
    saved_path: object,
    *,
    fallback: Path | None = None,
) -> Path:
    """Use a saved directory only while it is currently available."""

    fallback_path = (fallback or default_download_directory()).expanduser().resolve()
    if isinstance(saved_path, str) and saved_path.strip():
        try:
            candidate = Path(saved_path).expanduser().resolve()
            if candidate.is_dir():
                return candidate
        except (OSError, ValueError):
            pass
    return fallback_path


def load_app_settings(
    settings_file: Path = SETTINGS_FILE,
    *,
    legacy_file: Path | None = LEGACY_CONFIG_FILE,
    fallback_download_dir: Path | None = None,
) -> dict[str, object]:
    data: object = {}
    source = settings_file if settings_file.is_file() else legacy_file
    if source is not None and source.is_file():
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            LOGGER.exception("Could not load settings from %s", source)
            data = {}
    if isinstance(data, list):
        data = {"search_history": data}
    if not isinstance(data, dict):
        data = {}

    history = data.get("search_history", [])
    if not isinstance(history, list):
        history = []
    sort_column = data.get("sort_column")
    if not isinstance(sort_column, str) or sort_column not in SORT_COLUMNS:
        sort_column = None
    download_dir = resolve_download_directory(
        data.get("download_directory"),
        fallback=fallback_download_dir,
    )
    return {
        "search_history": [str(value) for value in history[:10]],
        "search_text": (
            data.get("search_text", "")
            if isinstance(data.get("search_text", ""), str)
            else ""
        ),
        "extension_filter": (
            data.get("extension_filter", "")
            if isinstance(data.get("extension_filter", ""), str)
            else ""
        ),
        "download_directory": str(download_dir),
        "sort_column": sort_column,
        "sort_descending": data.get("sort_descending") is True,
        "filename_filter": (
            data.get("filename_filter", "")
            if isinstance(data.get("filename_filter", ""), str)
            else ""
        ),
        "hide_downloaded": data.get("hide_downloaded") is True,
    }


def write_app_settings(settings_file: Path, settings: dict[str, object]) -> None:
    """Atomically persist settings so an interrupted write cannot corrupt them."""

    settings_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = settings_file.with_name(
        f"{settings_file.name}.{os.getpid()}.tmp"
    )
    try:
        temporary_file.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_file, settings_file)
    finally:
        try:
            if temporary_file.exists():
                temporary_file.unlink()
        except OSError:
            pass


_INVALID_WINDOWS_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_path_component(value: str, fallback: str) -> str:
    value = _INVALID_WINDOWS_CHARS.sub("_", value).strip().rstrip(". ")
    if value in ("", ".", ".."):
        return fallback
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        value = f"_{value}"
    return value


def archive_file_basename(archive_file: ArchiveFile) -> str:
    return archive_file.name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def file_destination(download_root: str, archive_file: ArchiveFile) -> Path:
    """Flatten Archive paths to one safe filename in the chosen directory."""

    raw_name = archive_file_basename(archive_file)
    filename = safe_path_component(raw_name, "archive_file")
    return Path(download_root).expanduser().resolve() / filename


def local_file_status(destination: Path, archive_file: ArchiveFile) -> str:
    if not destination.is_file():
        return "Ready"
    if archive_file.size is None:
        return "Downloaded (unverified)"
    try:
        actual_size = destination.stat().st_size
    except OSError:
        return "File error"
    if actual_size == archive_file.size:
        return "✓ Downloaded"
    return "⚠ Incomplete"


def local_status_is_complete(status: str) -> bool:
    return status in ("\u2713 Downloaded", "Downloaded (unverified)")


def local_file_is_complete(destination: Path, archive_file: ArchiveFile) -> bool:
    return local_status_is_complete(local_file_status(destination, archive_file))


def result_matches_filters(
    archive_file: ArchiveFile,
    *,
    filename_filter: str,
    hide_downloaded: bool,
    is_downloaded: bool,
) -> bool:
    basename = archive_file_basename(archive_file)
    if filename_filter.casefold() not in basename.casefold():
        return False
    if hide_downloaded and is_downloaded:
        return False
    return True


def result_sort_value(
    archive_file: ArchiveFile,
    column: str,
    *,
    row_number: int = 0,
    status: str = "",
) -> tuple[bool, int, object]:
    """Return a type-stable sort value; the first flag keeps unknowns last."""

    if column == "#0":
        return False, 0, row_number
    if column == "item":
        return False, 0, archive_file.identifier.casefold()
    if column == "file":
        return False, 0, archive_file.name.casefold()
    if column == "size":
        return archive_file.size is None, 0, archive_file.size or 0
    if column == "modified":
        if not archive_file.mtime:
            return True, 0, 0.0
        try:
            return False, 0, float(archive_file.mtime)
        except (TypeError, ValueError):
            try:
                parsed = dt.datetime.fromisoformat(
                    archive_file.mtime.replace("Z", "+00:00")
                )
                return False, 0, parsed.timestamp()
            except (TypeError, ValueError, OSError, OverflowError):
                return False, 1, archive_file.mtime.casefold()
    if column == "status":
        return False, 0, status.casefold()
    if column == "url":
        return False, 0, archive_file.download_url.casefold()
    return False, 0, archive_file.name.casefold()


@dataclass(slots=True)
class DownloadTask:
    token: int
    iid: str
    archive_file: ArchiveFile
    destination: Path
    size_bytes: int
    size_known: bool
    cancel_event: threading.Event


class DownloadCancelled(RuntimeError):
    pass


class PKGDownApp(ctk.CTk):
    EVENT_POLL_MS = 40
    RESULT_BATCH_SIZE = 200
    PAN_MULTIPLIER = 1.6
    EDGE_SCROLL_ZONE = 150
    EDGE_SCROLL_STEP = 14
    EDGE_SCROLL_INTERVAL_MS = 30
    WHEEL_SCROLL_STEP = 144
    TREE_ROW_HEIGHT = 29

    def __init__(
        self,
        *,
        settings_file: Path = SETTINGS_FILE,
        legacy_config_file: Path | None = LEGACY_CONFIG_FILE,
    ) -> None:
        super().__init__()
        ctk.set_appearance_mode("Dark")
        ctk.set_default_color_theme("blue")

        self.geometry("1220x760")
        self.minsize(900, 560)
        self.title("PKG_D0Wn — Archive.org File Browser")
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.settings_file = settings_file
        settings = load_app_settings(
            settings_file,
            legacy_file=legacy_config_file,
        )
        self.download_dir = ctk.StringVar(value=str(settings["download_directory"]))
        self.search_history = list(settings["search_history"])
        self.saved_search_text = str(settings["search_text"])
        self.saved_extension_filter = str(settings["extension_filter"])
        self.saved_filename_filter = str(settings["filename_filter"])
        self.saved_hide_downloaded = bool(settings["hide_downloaded"])

        self.ui_events: queue.Queue[tuple] = queue.Queue()
        self.download_queue: queue.Queue[DownloadTask] = queue.Queue()
        self.download_cancel_event = threading.Event()
        self.search_token = 0
        self.search_cancel_event: threading.Event | None = None
        self.search_active = False
        self.result_records: dict[str, ArchiveFile] = {}
        self.hidden_result_iids: set[str] = set()
        self.complete_result_iids: set[str] = set()
        self.result_count = 0
        self.sort_column = settings["sort_column"]
        self.sort_descending = bool(settings["sort_descending"])
        self.heading_labels = {
            "#0": "#",
            "item": "ARCHIVE ITEM",
            "file": "INDIVIDUAL FILE",
            "size": "SIZE",
            "modified": "MODIFIED",
            "status": "STATUS",
            "url": "DIRECT DOWNLOAD LINK",
        }
        self._pan_active = False
        self._pan_origin = (0, 0)
        self._pan_offset_v = 0.0
        self._pan_offset_h = 0.0
        self._pan_pointer = (0, 0)
        self._edge_direction_v = 0
        self._edge_direction_h = 0
        self._edge_scroll_job: str | None = None
        self.pending_downloads: set[tuple[int, str]] = set()
        self.queued_files = 0
        self.queued_bytes = 0
        self.queued_unknown_files = 0
        self.current_downloading = ""
        self.current_speed = 0.0
        self.current_download_task: DownloadTask | None = None
        self.current_downloaded_bytes = 0
        self.current_download_total_bytes = 0
        self.current_download_total_known = False
        self.download_stop_in_progress = False

        self.build_ui()
        self.save_settings()
        threading.Thread(
            target=self.download_worker,
            name="download-worker",
            daemon=True,
        ).start()
        self.after(self.EVENT_POLL_MS, self.process_ui_events)

    def build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="#12151C", corner_radius=12)
        header.pack(fill="x", padx=14, pady=(14, 8))

        search_row = ctk.CTkFrame(header, fg_color="transparent")
        search_row.pack(fill="x", padx=12, pady=(12, 6))

        self.search_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="Search Archive.org…",
            height=36,
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.search_entry.bind("<Return>", lambda _event: self.on_search())
        self.search_entry.bind("<FocusOut>", lambda _event: self.save_settings())
        if self.saved_search_text:
            self.search_entry.insert(0, self.saved_search_text)

        self.ext_entry = ctk.CTkEntry(
            search_row,
            placeholder_text="Extension (optional)",
            width=160,
            height=36,
        )
        self.ext_entry.pack(side="left", padx=(0, 8))
        self.ext_entry.bind("<Return>", lambda _event: self.on_search())
        self.ext_entry.bind("<FocusOut>", lambda _event: self.save_settings())
        if self.saved_extension_filter:
            self.ext_entry.insert(0, self.saved_extension_filter)

        history_values = self.search_history if self.search_history else ["No History"]
        self.history_opt = ctk.CTkOptionMenu(
            search_row,
            values=history_values,
            command=self.on_history_select,
            width=170,
            height=36,
        )
        self.history_opt.set("History")
        self.history_opt.pack(side="left", padx=(0, 8))

        self.search_btn = ctk.CTkButton(
            search_row,
            text="Search",
            command=self.on_search,
            width=105,
            height=36,
            fg_color="#6D5CE7",
            hover_color="#8070F4",
        )
        self.search_btn.pack(side="left", padx=(0, 8))

        self.cancel_btn = ctk.CTkButton(
            search_row,
            text="Cancel",
            command=self.cancel_search,
            state="disabled",
            width=90,
            height=36,
            fg_color="#313744",
            hover_color="#444C5D",
        )
        self.cancel_btn.pack(side="left")

        directory_row = ctk.CTkFrame(header, fg_color="transparent")
        directory_row.pack(fill="x", padx=12, pady=(0, 10))
        ctk.CTkLabel(
            directory_row,
            text="DOWNLOADS",
            text_color="#8F98A8",
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left", padx=(0, 10))
        self.dir_label = ctk.CTkLabel(
            directory_row,
            textvariable=self.download_dir,
            anchor="w",
            text_color="#D7DBE4",
        )
        self.dir_label.pack(side="left", fill="x", expand=True)
        self.browse_btn = ctk.CTkButton(
            directory_row,
            text="Browse",
            command=self.browse_dir,
            width=88,
            height=30,
        )
        self.browse_btn.pack(side="right")

        search_state = ctk.CTkFrame(self, fg_color="transparent")
        search_state.pack(fill="x", padx=18, pady=(0, 6))
        self.search_status_label = ctk.CTkLabel(
            search_state,
            text="Ready — leave Extension blank to list every file.",
            anchor="w",
            text_color="#AEB6C4",
        )
        self.search_status_label.pack(side="left", fill="x", expand=True)
        self.search_progress = ctk.CTkProgressBar(
            search_state,
            width=230,
            height=8,
            progress_color="#8B7CFF",
        )
        self.search_progress.set(0)
        self.search_progress.pack(side="right", padx=(10, 0))

        filter_row = ctk.CTkFrame(self, fg_color="transparent")
        filter_row.pack(fill="x", padx=18, pady=(0, 8))
        ctk.CTkLabel(
            filter_row,
            text="FILTER FILES",
            text_color="#8F98A8",
            font=("Segoe UI", 11, "bold"),
        ).pack(side="left", padx=(0, 10))
        self.filename_filter_var = tk.StringVar(value=self.saved_filename_filter)
        self.filename_filter_entry = ctk.CTkEntry(
            filter_row,
            textvariable=self.filename_filter_var,
            placeholder_text="Type part of a filename...",
            height=32,
        )
        self.filename_filter_entry.pack(
            side="left",
            fill="x",
            expand=True,
            padx=(0, 14),
        )
        self.hide_downloaded_var = tk.BooleanVar(value=self.saved_hide_downloaded)
        self.hide_downloaded_switch = ctk.CTkSwitch(
            filter_row,
            text="Hide downloaded",
            variable=self.hide_downloaded_var,
            onvalue=True,
            offvalue=False,
            command=self.on_result_filter_changed,
            width=150,
        )
        self.hide_downloaded_switch.pack(side="right")
        self.filename_filter_var.trace_add(
            "write",
            self.on_result_filter_changed,
        )

        table_shell = ctk.CTkFrame(self, fg_color="#12151C", corner_radius=12)
        table_shell.pack(fill="both", expand=True, padx=14, pady=(0, 8))

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Archive.Treeview",
            background="#161A22",
            fieldbackground="#161A22",
            foreground="#F4F6FA",
            rowheight=29,
            borderwidth=0,
            relief="flat",
        )
        style.map(
            "Archive.Treeview",
            background=[("selected", "#5045A5")],
            foreground=[("selected", "#FFFFFF")],
        )
        style.configure(
            "Archive.Treeview.Heading",
            background="#1D222C",
            foreground="#DCE1EA",
            bordercolor="#262C38",
            relief="flat",
            font=("Segoe UI", 10, "bold"),
        )
        style.map("Archive.Treeview.Heading", background=[("active", "#282E3A")])
        for orientation in ("Vertical", "Horizontal"):
            scrollbar_style = f"Archive.{orientation}.TScrollbar"
            style.configure(
                scrollbar_style,
                background="#333A47",
                troughcolor="#12151C",
                bordercolor="#12151C",
                lightcolor="#333A47",
                darkcolor="#333A47",
                arrowcolor="#333A47",
                gripcount=0,
                arrowsize=0,
                width=10,
            )
            style.map(
                scrollbar_style,
                background=[("pressed", "#8B7CFF"), ("active", "#454E5E")],
                lightcolor=[("pressed", "#8B7CFF"), ("active", "#454E5E")],
                darkcolor=[("pressed", "#8B7CFF"), ("active", "#454E5E")],
            )

        table_frame = tk.Frame(table_shell, bg="#12151C", highlightthickness=0)
        table_frame.pack(fill="both", expand=True, padx=10, pady=10)
        columns = ("item", "file", "size", "modified", "status", "url")
        self.results_tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="tree headings",
            selectmode="extended",
            style="Archive.Treeview",
        )
        for column, label in self.heading_labels.items():
            self.results_tree.heading(
                column,
                text=label,
                command=lambda selected_column=column: self.sort_results(selected_column),
            )
        self.results_tree.column("#0", width=60, minwidth=55, stretch=False, anchor="e")
        self.results_tree.column("item", width=190, minwidth=110)
        self.results_tree.column("file", width=280, minwidth=140)
        self.results_tree.column("size", width=90, minwidth=75, stretch=False, anchor="e")
        self.results_tree.column("modified", width=135, minwidth=110, stretch=False)
        self.results_tree.column("status", width=110, minwidth=90, stretch=False)
        self.results_tree.column("url", width=410, minwidth=180)

        vertical = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.results_tree.yview,
            style="Archive.Vertical.TScrollbar",
        )
        horizontal = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.results_tree.xview,
            style="Archive.Horizontal.TScrollbar",
        )
        self.results_tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.results_tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.results_tree.bind("<Button-1>", self.on_result_click)
        self.results_tree.bind("<<TreeviewSelect>>", lambda _event: self.update_result_count_label())
        self.results_tree.bind("<Button-2>", self.middle_drag_pan_down)
        self.results_tree.bind("<B2-Motion>", self.middle_drag_pan_move)
        self.results_tree.bind("<ButtonRelease-2>", self.middle_drag_pan_up)
        self.results_tree.bind("<MouseWheel>", self.result_mouse_wheel)
        self.results_tree.bind("<Shift-MouseWheel>", self.result_shift_mouse_wheel)
        self.results_tree.bind("<Escape>", lambda _event: self.end_middle_drag())
        self.results_tree.bind("<Return>", lambda _event: self.queue_selected_downloads())

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=14, pady=(0, 8))
        self.result_count_label = ctk.CTkLabel(
            actions,
            text="0 files",
            text_color="#AEB6C4",
            anchor="w",
        )
        self.result_count_label.pack(side="left", padx=(4, 12))
        ctk.CTkButton(
            actions,
            text="Download Selected",
            command=self.queue_selected_downloads,
            width=145,
        ).pack(side="left", padx=(0, 7))
        self.stop_downloads_btn = ctk.CTkButton(
            actions,
            text="Stop Downloads",
            command=self.stop_downloads,
            state="disabled",
            width=130,
            fg_color="#702F3A",
            hover_color="#8C3A48",
        )
        self.stop_downloads_btn.pack(side="left", padx=(0, 7))
        ctk.CTkButton(
            actions,
            text="Delete Downloaded",
            command=self.delete_selected_downloads,
            width=145,
            fg_color="#702F3A",
            hover_color="#8C3A48",
        ).pack(side="left", padx=(0, 7))
        ctk.CTkButton(
            actions,
            text="Copy Selected Links",
            command=self.copy_selected_links,
            width=155,
            fg_color="#313744",
            hover_color="#444C5D",
        ).pack(side="right", padx=(7, 4))
        ctk.CTkButton(
            actions,
            text="Copy All Links",
            command=self.copy_all_links,
            width=125,
            fg_color="#313744",
            hover_color="#444C5D",
        ).pack(side="right")

        storage_preview = ctk.CTkFrame(self, fg_color="transparent")
        storage_preview.pack(fill="x", padx=18, pady=(0, 8))
        self.selected_space_label = ctk.CTkLabel(
            storage_preview,
            text="Selected: 0 B",
            anchor="w",
            text_color="#AEB6C4",
        )
        self.selected_space_label.pack(side="left", fill="x", expand=True)

        bottom = ctk.CTkFrame(self, height=36, fg_color="#12151C", corner_radius=10)
        bottom.pack(side="bottom", fill="x", padx=14, pady=(0, 12))
        self.status_file_label = ctk.CTkLabel(bottom, text="Idle", anchor="w")
        self.status_file_label.pack(side="left", fill="x", expand=True, padx=12)
        self.status_speed_label = ctk.CTkLabel(bottom, text="0 KB/s", width=95, anchor="e")
        self.status_speed_label.pack(side="left", padx=8)
        self.status_queued_label = ctk.CTkLabel(bottom, text="Queued: 0", width=100, anchor="e")
        self.status_queued_label.pack(side="left", padx=8)
        self.status_size_label = ctk.CTkLabel(bottom, text="Remaining: 0 B", width=145, anchor="e")
        self.status_size_label.pack(side="left", padx=8)
        self.status_current_eta_label = ctk.CTkLabel(
            bottom,
            text="Current ETA: —",
            width=125,
            anchor="e",
        )
        self.status_current_eta_label.pack(side="left", padx=8)
        self.status_all_eta_label = ctk.CTkLabel(
            bottom,
            text="All ETA: —",
            width=115,
            anchor="e",
        )
        self.status_all_eta_label.pack(side="left", padx=(8, 12))
        self.update_selection_space_preview()
        self.apply_result_filters(persist=False)

    def browse_dir(self) -> None:
        directory = filedialog.askdirectory(initialdir=self.download_dir.get())
        if directory:
            self.download_dir.set(directory)
            self.save_settings()
            self.refresh_download_statuses()
            self.update_selection_space_preview()

    def ensure_available_download_directory(self) -> str:
        current = self.download_dir.get()
        resolved = resolve_download_directory(current)
        if str(resolved) != current:
            self.download_dir.set(str(resolved))
            self.save_settings()
            self.refresh_download_statuses()
            self.search_status_label.configure(
                text=f"The saved download folder is unavailable. Using {resolved}.",
                text_color="#E7B45A",
            )
        return str(resolved)

    def on_search(self) -> None:
        query = self.search_entry.get().strip()
        if not query:
            self.search_status_label.configure(text="Enter a search first.", text_color="#E7B45A")
            return
        extension = self.ext_entry.get().strip()

        if query not in self.search_history:
            self.search_history.insert(0, query)
            self.search_history = self.search_history[:10]
            self.history_opt.configure(values=self.search_history)
        self.save_settings()

        # A new search invalidates all queued display events from the old search.
        if self.search_cancel_event is not None:
            self.search_cancel_event.set()
        self.search_token += 1
        token = self.search_token
        cancel_event = threading.Event()
        self.search_cancel_event = cancel_event
        self.search_active = True

        self.clear_results()
        self.search_progress.set(0)
        self.search_status_label.configure(
            text="Contacting Archive.org…",
            text_color="#AEB6C4",
        )
        self.search_btn.configure(text="New Search")
        self.cancel_btn.configure(state="normal", text="Cancel")

        LOGGER.info("Searching for %r with extension filter %r", query, extension)
        threading.Thread(
            target=self.perform_search,
            args=(token, query, extension, cancel_event),
            name=f"archive-search-{token}",
            daemon=True,
        ).start()

    def perform_search(
        self,
        token: int,
        query: str,
        extension: str,
        cancel_event: threading.Event,
    ) -> None:
        client = ArchiveOrgClient(logger=LOGGER)
        stats = SearchStats()
        batch: list[ArchiveFile] = []

        def report_progress(snapshot: SearchStats) -> None:
            self.ui_events.put(("search_progress", token, snapshot))

        def report_warning(identifier: str, message: str) -> None:
            self.ui_events.put(("search_warning", token, identifier, message))

        try:
            for archive_file in client.iter_files(
                query,
                extension=extension,
                cancel_event=cancel_event,
                progress=report_progress,
                warning=report_warning,
                stats=stats,
            ):
                batch.append(archive_file)
                if len(batch) >= self.RESULT_BATCH_SIZE:
                    self.ui_events.put(("search_batch", token, batch))
                    batch = []
            if batch:
                self.ui_events.put(("search_batch", token, batch))
            self.ui_events.put(("search_done", token, stats.snapshot()))
        except SearchCancelled:
            self.ui_events.put(("search_cancelled", token))
        except (ArchiveSearchError, ValueError) as exc:
            LOGGER.exception("Search failed")
            self.ui_events.put(("search_error", token, str(exc)))
        except Exception as exc:
            LOGGER.exception("Unexpected search failure")
            self.ui_events.put(
                ("search_error", token, f"Unexpected search error: {exc}")
            )

    def cancel_search(self) -> None:
        if self.search_cancel_event is not None:
            self.search_cancel_event.set()
            self.cancel_btn.configure(state="disabled", text="Cancelling…")
            self.search_status_label.configure(text="Cancelling search…")

    def process_ui_events(self) -> None:
        try:
            # Process bounded batches so downloads and window input keep flowing even
            # when a search returns hundreds of thousands of file records.
            for _ in range(300):
                try:
                    event = self.ui_events.get_nowait()
                except queue.Empty:
                    break
                self.handle_ui_event(event)
        except tk.TclError:
            return
        self.after(self.EVENT_POLL_MS, self.process_ui_events)

    def handle_ui_event(self, event: tuple) -> None:
        event_type = event[0]
        if event_type.startswith("search_"):
            token = event[1]
            if token != self.search_token:
                return
            if event_type == "search_batch":
                self.add_result_batch(event[2])
            elif event_type == "search_progress":
                self.update_search_progress(event[2])
            elif event_type == "search_warning":
                identifier, message = event[2], event[3]
                LOGGER.warning("Skipped Archive item %s: %s", identifier, message)
            elif event_type == "search_done":
                self.finish_search(event[2])
            elif event_type == "search_cancelled":
                self.search_active = False
                self.search_status_label.configure(
                    text=f"Search cancelled — {self.result_count:,} files were loaded.",
                    text_color="#E7B45A",
                )
                self.reset_search_buttons()
            elif event_type == "search_error":
                self.search_active = False
                self.search_status_label.configure(text=event[2], text_color="#F07178")
                self.reset_search_buttons()
                messagebox.showerror("Archive.org search failed", event[2])
            return

        if event_type == "download_start":
            task, display_name = event[1:3]
            self.current_download_task = task
            self.current_downloading = display_name
            self.current_speed = 0.0
            self.current_downloaded_bytes = 0
            self.current_download_total_bytes = task.size_bytes
            self.current_download_total_known = task.size_known
            self.set_row_status(task.token, task.iid, "Downloading 0%")
            self.update_bottom_bar()
        elif event_type == "download_progress":
            task, fraction, speed, downloaded, total_length, total_known = event[1:7]
            if self.current_download_task is not task:
                self.current_download_task = task
            self.current_speed = speed
            self.current_downloaded_bytes = downloaded
            self.current_download_total_bytes = total_length
            self.current_download_total_known = total_known
            percent = max(0, min(100, int(fraction * 100))) if fraction is not None else 0
            status = f"Downloading {percent}%" if fraction is not None else "Downloading"
            self.set_row_status(task.token, task.iid, status)
            self.update_bottom_bar()
        elif event_type in ("download_done", "download_cancelled", "download_error"):
            task = event[1]
            self.pending_downloads.discard((task.token, task.iid))
            self.remove_task_from_totals(task)
            if self.current_download_task is task:
                self.current_download_task = None
                self.current_downloading = ""
                self.current_speed = 0.0
                self.current_downloaded_bytes = 0
                self.current_download_total_bytes = 0
                self.current_download_total_known = False
            if event_type == "download_done":
                self.set_row_status(task.token, task.iid, "\u2713 Downloaded")
                if task.token == self.search_token:
                    self.complete_result_iids.add(task.iid)
            elif event_type == "download_cancelled":
                self.set_row_status(task.token, task.iid, "Stopped")
                if task.token == self.search_token:
                    self.complete_result_iids.discard(task.iid)
            else:
                error_message = event[2]
                self.set_row_status(task.token, task.iid, "Error")
                if task.token == self.search_token:
                    self.complete_result_iids.discard(task.iid)
                LOGGER.error("Download failed: %s", error_message)
                self.status_file_label.configure(text=f"Download error: {error_message}")
            if self.current_download_task is None:
                self.download_stop_in_progress = False
            self.update_bottom_bar(keep_error=event_type == "download_error")
            self.apply_result_filters((task.iid,))
            self.update_selection_space_preview()

    def reset_search_buttons(self) -> None:
        self.search_btn.configure(text="Search")
        self.cancel_btn.configure(state="disabled", text="Cancel")
        self.search_cancel_event = None

    def update_search_progress(self, stats: SearchStats) -> None:
        if stats.total_items:
            fraction = min(1.0, stats.processed_items / stats.total_items)
            self.search_progress.set(fraction)
            message = (
                f"Read {stats.processed_items:,}/{stats.total_items:,} items — "
                f"{stats.files_found:,} individual files"
            )
        else:
            self.search_progress.set(0)
            message = "No matching Archive.org items found."
        if stats.failed_items:
            message += f" — {stats.failed_items:,} item errors"
        self.search_status_label.configure(text=message, text_color="#AEB6C4")

    def finish_search(self, stats: SearchStats) -> None:
        self.search_active = False
        self.search_progress.set(1 if stats.total_items else 0)
        if self.sort_column is not None:
            self.apply_result_sort()
        if stats.failed_items:
            message = (
                f"Loaded {stats.files_found:,} files from "
                f"{stats.processed_items - stats.failed_items:,} items; "
                f"{stats.failed_items:,} items failed after retries. Retry for a complete list."
            )
            color = "#E7B45A"
        else:
            message = (
                f"Complete — {stats.files_found:,} individual files from "
                f"{stats.processed_items:,} Archive.org items across "
                f"{stats.pages_fetched:,} search pages."
            )
            color = "#82C995"
        self.search_status_label.configure(text=message, text_color=color)
        self.reset_search_buttons()
        LOGGER.info(message)

    def add_result_batch(self, records: list[ArchiveFile]) -> None:
        added_iids: list[str] = []
        for archive_file in records:
            self.result_count += 1
            iid = f"result-{self.result_count}"
            self.result_records[iid] = archive_file
            destination = file_destination(self.download_dir.get(), archive_file)
            status = local_file_status(destination, archive_file)
            if local_status_is_complete(status):
                self.complete_result_iids.add(iid)
            self.results_tree.insert(
                "",
                "end",
                iid=iid,
                text=f"{self.result_count:,}",
                values=(
                    archive_file.identifier,
                    archive_file.name,
                    format_bytes(archive_file.size),
                    format_mtime(archive_file.mtime),
                    status,
                    archive_file.download_url,
                ),
            )
            added_iids.append(iid)
        self.apply_result_filters(added_iids, resort=False)

    def refresh_download_statuses(self) -> None:
        for iid, archive_file in self.result_records.items():
            if not self.results_tree.exists(iid):
                continue
            destination = file_destination(self.download_dir.get(), archive_file)
            status = local_file_status(destination, archive_file)
            self.set_current_row_status(iid, status)
            if local_status_is_complete(status):
                self.complete_result_iids.add(iid)
            else:
                self.complete_result_iids.discard(iid)
        self.apply_result_filters()
        self.update_selection_space_preview()

    def selected_iids(self) -> tuple[str, ...]:
        return tuple(self.results_tree.selection())

    def update_result_count_label(self) -> None:
        selected_count = len(self.results_tree.selection())
        visible_count = len(self.results_tree.get_children(""))
        if visible_count == self.result_count:
            text = f"{self.result_count:,} files"
        else:
            text = f"{visible_count:,} shown / {self.result_count:,} files"
        if selected_count:
            text += f"  •  {selected_count:,} selected"
        self.result_count_label.configure(text=text)
        if hasattr(self, "selected_space_label"):
            self.update_selection_space_preview()

    def update_selection_space_preview(self) -> None:
        selected_entries = [
            (
                self.result_records[iid],
                file_destination(self.download_dir.get(), self.result_records[iid]),
            )
            for iid in self.selected_iids()
            if iid in self.result_records
        ]
        pending_files = [
            archive_file
            for archive_file, destination in selected_entries
            if not local_file_is_complete(destination, archive_file)
        ]
        complete_count = len(selected_entries) - len(pending_files)
        known_bytes, unknown_count = selected_size_totals(pending_files)
        unknown_text = (
            f" + {unknown_count:,} unknown-size file(s)" if unknown_count else ""
        )
        complete_text = (
            f"  |  {complete_count:,} already complete" if complete_count else ""
        )
        selected_text = (
            f"Selected to download: {format_bytes(known_bytes)}{unknown_text}"
            f"{complete_text}"
        )
        try:
            volume, free_bytes = storage_volume_info(self.download_dir.get())
        except (OSError, ValueError) as exc:
            self.selected_space_label.configure(
                text=f"{selected_text}  |  Free space unavailable: {exc}",
                text_color="#E7B45A",
            )
            return

        if known_bytes > free_bytes:
            remaining_text = f"Short by {format_bytes(known_bytes - free_bytes)}"
            color = "#F07178"
        else:
            remaining_text = f"After known sizes: {format_bytes(free_bytes - known_bytes)}"
            color = "#AEB6C4"
        if unknown_count:
            remaining_text += " (unknown sizes not included)"
        self.selected_space_label.configure(
            text=(
                f"{selected_text}  |  Free on {volume}: {format_bytes(free_bytes)}"
                f"  |  {remaining_text}"
            ),
            text_color=color,
        )

    def on_result_filter_changed(self, *_args) -> None:
        if hasattr(self, "results_tree"):
            self.apply_result_filters(persist=True)

    def apply_result_filters(
        self,
        iids: tuple[str, ...] | list[str] | None = None,
        *,
        persist: bool = False,
        resort: bool = True,
    ) -> None:
        filename_filter = self.filename_filter_var.get()
        hide_downloaded = bool(self.hide_downloaded_var.get())
        targets = tuple(self.result_records) if iids is None else tuple(iids)

        for iid in targets:
            archive_file = self.result_records.get(iid)
            if archive_file is None or not self.results_tree.exists(iid):
                continue
            should_show = result_matches_filters(
                archive_file,
                filename_filter=filename_filter,
                hide_downloaded=hide_downloaded,
                is_downloaded=iid in self.complete_result_iids,
            )
            is_hidden = iid in self.hidden_result_iids
            if should_show and is_hidden:
                self.results_tree.reattach(iid, "", "end")
                self.hidden_result_iids.discard(iid)
            elif not should_show and not is_hidden:
                self.results_tree.selection_remove(iid)
                self.results_tree.detach(iid)
                self.hidden_result_iids.add(iid)

        if self.sort_column is not None and resort:
            self.apply_result_sort()
        elif self.sort_column is None and resort:
            ordered = sorted(
                self.results_tree.get_children(""),
                key=lambda iid: int(iid.rsplit("-", 1)[-1]),
            )
            for position, iid in enumerate(ordered):
                self.results_tree.move(iid, "", position)
            self.update_result_count_label()
        else:
            self.update_result_count_label()
        if persist:
            self.save_settings()

    def clear_results(self) -> None:
        for iid in tuple(self.result_records):
            if self.results_tree.exists(iid):
                self.results_tree.delete(iid)
        self.result_records.clear()
        self.hidden_result_iids.clear()
        self.complete_result_iids.clear()
        self.result_count = 0
        self.update_result_count_label()

    def toggle_result_selection(self, iid: str) -> None:
        if not iid or not self.results_tree.exists(iid):
            return
        if iid in self.results_tree.selection():
            self.results_tree.selection_remove(iid)
        else:
            self.results_tree.selection_add(iid)
            self.results_tree.focus(iid)
        self.update_result_count_label()

    def on_result_click(self, event) -> str | None:
        region = self.results_tree.identify_region(event.x, event.y)
        if region not in ("tree", "cell"):
            return None
        iid = self.results_tree.identify_row(event.y)
        if iid:
            self.toggle_result_selection(iid)
        # Prevent ttk's class binding from clearing the independently toggled rows.
        return "break"

    def sort_results(self, column: str) -> None:
        if column == self.sort_column:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_column = column
            self.sort_descending = False
        self.apply_result_sort()
        label = self.heading_labels.get(column, column)
        direction = "descending" if self.sort_descending else "ascending"
        self.search_status_label.configure(
            text=f"Sorted by {label.title()} — {direction}.",
            text_color="#AEB6C4",
        )
        self.save_settings()

    def apply_result_sort(self) -> None:
        column = self.sort_column
        if column is None:
            return
        known: list[tuple[tuple[int, object], str]] = []
        unknown: list[str] = []
        for iid in self.results_tree.get_children(""):
            archive_file = self.result_records.get(iid)
            if archive_file is None:
                continue
            values = self.results_tree.item(iid, "values")
            status = str(values[4]) if len(values) > 4 else ""
            try:
                row_number = int(iid.rsplit("-", 1)[-1])
            except (TypeError, ValueError):
                row_number = 0
            missing, value_type, value = result_sort_value(
                archive_file,
                column,
                row_number=row_number,
                status=status,
            )
            if missing:
                unknown.append(iid)
            else:
                known.append(((value_type, value), iid))
        known.sort(key=lambda entry: entry[0], reverse=self.sort_descending)
        ordered = [iid for _key, iid in known] + unknown
        for position, iid in enumerate(ordered):
            self.results_tree.move(iid, "", position)
        for heading, label in self.heading_labels.items():
            indicator = ""
            if heading == column:
                indicator = "  ▼" if self.sort_descending else "  ▲"
            self.results_tree.heading(heading, text=label + indicator)
        self.update_result_count_label()

    def _tree_content_dimensions(self) -> tuple[float, float]:
        row_count = len(self.results_tree.get_children(""))
        content_height = max(
            float(self.results_tree.winfo_height()),
            float(row_count * self.TREE_ROW_HEIGHT),
        )
        columns = ("#0",) + tuple(self.results_tree.cget("columns"))
        content_width = max(
            float(self.results_tree.winfo_width()),
            float(sum(int(self.results_tree.column(column, "width")) for column in columns)),
        )
        return content_height, content_width

    def _tree_scroll_offsets(self) -> tuple[float, float]:
        content_height, content_width = self._tree_content_dimensions()
        yview = self.results_tree.yview()
        xview = self.results_tree.xview()
        vertical = (yview[0] * content_height) if yview else 0.0
        horizontal = (xview[0] * content_width) if xview else 0.0
        return vertical, horizontal

    def _tree_scroll_to(self, vertical: float, horizontal: float) -> None:
        content_height, content_width = self._tree_content_dimensions()
        viewport_height = float(self.results_tree.winfo_height())
        viewport_width = float(self.results_tree.winfo_width())
        max_vertical = max(0.0, content_height - viewport_height)
        max_horizontal = max(0.0, content_width - viewport_width)
        vertical = max(0.0, min(max_vertical, vertical))
        horizontal = max(0.0, min(max_horizontal, horizontal))
        self.results_tree.yview_moveto(vertical / content_height if content_height else 0.0)
        self.results_tree.xview_moveto(horizontal / content_width if content_width else 0.0)

    def _tree_scroll_by(self, vertical: float = 0.0, horizontal: float = 0.0) -> None:
        current_vertical, current_horizontal = self._tree_scroll_offsets()
        self._tree_scroll_to(
            current_vertical + vertical,
            current_horizontal + horizontal,
        )

    def _tree_can_scroll(self) -> tuple[bool, bool]:
        yview = self.results_tree.yview()
        xview = self.results_tree.xview()
        can_vertical = bool(yview and yview[1] - yview[0] < 0.999)
        can_horizontal = bool(xview and xview[1] - xview[0] < 0.999)
        return can_vertical, can_horizontal

    def middle_drag_pan_down(self, event) -> str:
        can_vertical, can_horizontal = self._tree_can_scroll()
        if not can_vertical and not can_horizontal:
            return "break"
        self._pan_active = True
        self._pan_origin = (event.x_root, event.y_root)
        self._pan_pointer = self._pan_origin
        self._pan_offset_v, self._pan_offset_h = self._tree_scroll_offsets()
        self.results_tree.configure(cursor="fleur")
        try:
            self.results_tree.grab_set()
        except tk.TclError:
            pass
        return "break"

    def middle_drag_pan_move(self, event) -> str:
        if not self._pan_active:
            return "break"
        self._pan_pointer = (event.x_root, event.y_root)
        self.update_edge_auto_scroll(event.x_root, event.y_root)
        target_vertical = self._pan_offset_v
        target_horizontal = self._pan_offset_h
        if self._edge_direction_v == 0:
            target_vertical += (event.y_root - self._pan_origin[1]) * self.PAN_MULTIPLIER
        if self._edge_direction_h == 0:
            target_horizontal += (event.x_root - self._pan_origin[0]) * self.PAN_MULTIPLIER
        self._tree_scroll_to(target_vertical, target_horizontal)
        return "break"

    def middle_drag_pan_up(self, _event=None) -> str:
        self.end_middle_drag()
        return "break"

    def update_edge_auto_scroll(self, pointer_x: int, pointer_y: int) -> None:
        window_x = pointer_x - self.winfo_rootx()
        window_y = pointer_y - self.winfo_rooty()
        window_width = self.winfo_width()
        window_height = self.winfo_height()
        can_vertical, can_horizontal = self._tree_can_scroll()
        self._edge_direction_v = (
            -1
            if can_vertical and window_y <= self.EDGE_SCROLL_ZONE
            else 1
            if can_vertical and window_y >= window_height - self.EDGE_SCROLL_ZONE
            else 0
        )
        self._edge_direction_h = (
            -1
            if can_horizontal and window_x <= self.EDGE_SCROLL_ZONE
            else 1
            if can_horizontal and window_x >= window_width - self.EDGE_SCROLL_ZONE
            else 0
        )
        if self._edge_direction_v == 0 and self._edge_direction_h == 0:
            self.cancel_edge_scroll()
        elif self._edge_scroll_job is None:
            self._edge_scroll_job = self.after(
                self.EDGE_SCROLL_INTERVAL_MS,
                self.edge_auto_scroll_tick,
            )

    def edge_auto_scroll_tick(self) -> None:
        self._edge_scroll_job = None
        if not self._pan_active:
            return
        self._tree_scroll_by(
            vertical=self._edge_direction_v * self.EDGE_SCROLL_STEP,
            horizontal=self._edge_direction_h * self.EDGE_SCROLL_STEP,
        )
        # Match Sink's re-anchor: the next tiny move cannot snap back against
        # the timer-driven offset.
        self._pan_offset_v, self._pan_offset_h = self._tree_scroll_offsets()
        self._pan_origin = self._pan_pointer
        if self._edge_direction_v or self._edge_direction_h:
            self._edge_scroll_job = self.after(
                self.EDGE_SCROLL_INTERVAL_MS,
                self.edge_auto_scroll_tick,
            )

    def cancel_edge_scroll(self) -> None:
        if self._edge_scroll_job is not None:
            try:
                self.after_cancel(self._edge_scroll_job)
            except tk.TclError:
                pass
            self._edge_scroll_job = None

    def end_middle_drag(self) -> None:
        self._pan_active = False
        self._edge_direction_v = 0
        self._edge_direction_h = 0
        self.cancel_edge_scroll()
        if hasattr(self, "results_tree"):
            try:
                if self.grab_current() == self.results_tree:
                    self.results_tree.grab_release()
                self.results_tree.configure(cursor="")
            except tk.TclError:
                pass

    def result_mouse_wheel(self, event) -> str:
        notches = event.delta / 120 if event.delta else 0
        self._tree_scroll_by(vertical=-notches * self.WHEEL_SCROLL_STEP)
        return "break"

    def result_shift_mouse_wheel(self, event) -> str:
        notches = event.delta / 120 if event.delta else 0
        self._tree_scroll_by(horizontal=-notches * self.WHEEL_SCROLL_STEP)
        return "break"

    def queue_selected_downloads(self) -> None:
        selected = self.selected_iids()
        if not selected:
            self.search_status_label.configure(
                text="Select one or more files to download.",
                text_color="#E7B45A",
            )
            return
        download_root = self.ensure_available_download_directory()
        added = 0
        for iid in selected:
            archive_file = self.result_records.get(iid)
            if archive_file is None:
                continue
            task_key = (self.search_token, iid)
            if task_key in self.pending_downloads:
                continue
            destination = file_destination(download_root, archive_file)
            if local_file_is_complete(destination, archive_file):
                self.set_current_row_status(iid, local_file_status(destination, archive_file))
                self.complete_result_iids.add(iid)
                continue
            size_bytes = archive_file.size or 0
            size_known = archive_file.size is not None
            task = DownloadTask(
                token=self.search_token,
                iid=iid,
                archive_file=archive_file,
                destination=destination,
                size_bytes=size_bytes,
                size_known=size_known,
                cancel_event=self.download_cancel_event,
            )
            self.pending_downloads.add(task_key)
            self.download_queue.put(task)
            self.queued_files += 1
            if size_known:
                self.queued_bytes += size_bytes
            else:
                self.queued_unknown_files += 1
            self.set_current_row_status(iid, "Queued")
            self.complete_result_iids.discard(iid)
            added += 1
        if added:
            self.download_stop_in_progress = False
        self.update_bottom_bar()
        if added:
            self.search_status_label.configure(
                text=f"Queued {added:,} file(s) for download.",
                text_color="#AEB6C4",
            )
        self.apply_result_filters(selected)

    def remove_task_from_totals(self, task: DownloadTask) -> None:
        self.queued_files = max(0, self.queued_files - 1)
        if task.size_known:
            self.queued_bytes = max(0, self.queued_bytes - task.size_bytes)
        else:
            self.queued_unknown_files = max(0, self.queued_unknown_files - 1)

    def stop_downloads(self) -> None:
        if self.queued_files <= 0:
            return

        stopping_event = self.download_cancel_event
        stopping_event.set()
        cancelled_queued = 0
        while True:
            try:
                task = self.download_queue.get_nowait()
            except queue.Empty:
                break
            task.cancel_event.set()
            self.pending_downloads.discard((task.token, task.iid))
            self.remove_task_from_totals(task)
            self.set_row_status(task.token, task.iid, "Stopped")
            self.download_queue.task_done()
            cancelled_queued += 1

        # New downloads get a fresh event, so cancelling this batch does not
        # require restarting the application or its worker thread.
        self.download_cancel_event = threading.Event()
        active_stopping = self.current_download_task is not None
        self.download_stop_in_progress = active_stopping
        if active_stopping:
            message = "Stopping the current download"
            if cancelled_queued:
                message += f" and cleared {cancelled_queued:,} queued file(s)"
            message += "..."
        else:
            message = f"Stopped {cancelled_queued:,} queued download(s)."
        self.search_status_label.configure(text=message, text_color="#E7B45A")
        self.update_bottom_bar()

    def download_worker(self) -> None:
        session = create_retry_session()
        session.headers["Accept"] = "*/*"
        while True:
            task = self.download_queue.get()
            archive_file = task.archive_file
            display_name = f"{archive_file.identifier}/{archive_file.name}"
            self.ui_events.put(("download_start", task, display_name))
            part_path = task.destination.with_name(task.destination.name + ".part")
            try:
                if task.cancel_event.is_set():
                    raise DownloadCancelled()
                task.destination.parent.mkdir(parents=True, exist_ok=True)
                with session.get(
                    archive_file.download_url,
                    stream=True,
                    timeout=(12, 30),
                ) as response:
                    response.raise_for_status()
                    header_size = response.headers.get("content-length")
                    try:
                        total_length = int(header_size) if header_size else task.size_bytes
                    except (TypeError, ValueError):
                        total_length = task.size_bytes
                    total_known = bool(header_size) or task.size_known
                    downloaded = 0
                    started = time.monotonic()
                    last_report = started
                    self.ui_events.put(
                        (
                            "download_progress",
                            task,
                            0.0 if total_known and total_length > 0 else None,
                            0.0,
                            downloaded,
                            total_length,
                            total_known,
                        )
                    )
                    with part_path.open("wb") as destination_file:
                        for chunk in response.iter_content(chunk_size=256 * 1024):
                            if task.cancel_event.is_set():
                                raise DownloadCancelled()
                            if not chunk:
                                continue
                            destination_file.write(chunk)
                            downloaded += len(chunk)
                            now = time.monotonic()
                            if now - last_report >= 0.25:
                                elapsed = max(now - started, 0.001)
                                speed = (downloaded / 1024) / elapsed
                                fraction = (
                                    min(1.0, downloaded / total_length)
                                    if total_length > 0
                                    else None
                                )
                                self.ui_events.put(
                                    (
                                        "download_progress",
                                        task,
                                        fraction,
                                        speed,
                                        downloaded,
                                        total_length,
                                        total_known,
                                    )
                                )
                                last_report = now
                if task.cancel_event.is_set():
                    raise DownloadCancelled()
                os.replace(part_path, task.destination)
                self.ui_events.put(("download_done", task))
            except DownloadCancelled:
                try:
                    if part_path.exists():
                        part_path.unlink()
                except OSError:
                    LOGGER.warning("Could not remove partial file %s", part_path)
                self.ui_events.put(("download_cancelled", task))
            except Exception as exc:
                LOGGER.exception("Download error for %s", display_name)
                try:
                    if part_path.exists():
                        part_path.unlink()
                except OSError:
                    LOGGER.warning("Could not remove partial file %s", part_path)
                self.ui_events.put(("download_error", task, str(exc)))
            finally:
                self.download_queue.task_done()

    def delete_selected_downloads(self) -> None:
        selected = self.selected_iids()
        if not selected:
            self.search_status_label.configure(
                text="Select downloaded files to delete.",
                text_color="#E7B45A",
            )
            return
        deleted = 0
        errors = []
        for iid in selected:
            archive_file = self.result_records.get(iid)
            if archive_file is None or (self.search_token, iid) in self.pending_downloads:
                continue
            destination = file_destination(self.download_dir.get(), archive_file)
            try:
                if destination.is_file():
                    destination.unlink()
                    deleted += 1
                self.set_current_row_status(iid, "Ready")
                self.complete_result_iids.discard(iid)
            except OSError as exc:
                errors.append(f"{archive_file.name}: {exc}")
                self.set_current_row_status(iid, "Delete error")
        if errors:
            messagebox.showerror("Could not delete files", "\n".join(errors[:10]))
        self.search_status_label.configure(
            text=f"Deleted {deleted:,} downloaded file(s).",
            text_color="#AEB6C4" if not errors else "#F07178",
        )
        self.apply_result_filters(selected)
        self.update_selection_space_preview()

    def copy_selected_links(self) -> None:
        selected = self.selected_iids()
        if not selected:
            self.search_status_label.configure(
                text="Select files whose links should be copied.",
                text_color="#E7B45A",
            )
            return
        self.copy_links(selected)

    def copy_all_links(self) -> None:
        self.copy_links(tuple(self.result_records))

    def copy_links(self, iids: tuple[str, ...]) -> None:
        links = [
            self.result_records[iid].download_url
            for iid in iids
            if iid in self.result_records
        ]
        if not links:
            self.search_status_label.configure(
                text="There are no links to copy.",
                text_color="#E7B45A",
            )
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(links))
        self.search_status_label.configure(
            text=f"Copied {len(links):,} direct file links to the clipboard.",
            text_color="#82C995",
        )

    def set_row_status(self, token: int, iid: str, status: str) -> None:
        if token == self.search_token:
            self.set_current_row_status(iid, status)

    def set_current_row_status(self, iid: str, status: str) -> None:
        if not self.results_tree.exists(iid):
            return
        values = list(self.results_tree.item(iid, "values"))
        if len(values) >= 5:
            values[4] = status
            self.results_tree.item(iid, values=values)

    def update_bottom_bar(self, *, keep_error: bool = False) -> None:
        if not keep_error:
            self.status_file_label.configure(
                text=f"Downloading: {self.current_downloading}"
                if self.current_downloading
                else "Idle"
            )
        self.status_speed_label.configure(text=f"{self.current_speed:,.1f} KB/s")
        self.status_queued_label.configure(text=f"Queued: {self.queued_files:,}")

        current_task = self.current_download_task
        known_remaining = self.queued_bytes
        unknown_remaining = self.queued_unknown_files
        current_remaining: int | None = None
        if current_task is not None:
            if current_task.size_known:
                known_remaining = max(0, known_remaining - current_task.size_bytes)
            elif self.current_download_total_known:
                unknown_remaining = max(0, unknown_remaining - 1)
            if self.current_download_total_known:
                current_remaining = max(
                    0,
                    self.current_download_total_bytes - self.current_downloaded_bytes,
                )
                known_remaining += current_remaining

        unknown_suffix = (
            f" + {unknown_remaining:,} unknown" if unknown_remaining else ""
        )
        self.status_size_label.configure(
            text=f"Remaining: {format_bytes(known_remaining)}{unknown_suffix}"
        )

        if current_task is None:
            current_eta_text = "Waiting..." if self.queued_files else "\u2014"
        elif current_remaining is None:
            current_eta_text = "\u2014"
        elif self.current_speed <= 0:
            current_eta_text = "Calculating..."
        else:
            current_eta_text = format_eta(calculate_eta(current_remaining, self.current_speed))

        if not self.queued_files:
            all_eta_text = "\u2014"
        elif unknown_remaining or (current_task and current_remaining is None):
            all_eta_text = "\u2014 (unknown size)"
        elif self.current_speed <= 0:
            all_eta_text = "Calculating..."
        else:
            all_eta_text = format_eta(calculate_eta(known_remaining, self.current_speed))
        self.status_current_eta_label.configure(text=f"Current ETA: {current_eta_text}")
        self.status_all_eta_label.configure(text=f"All ETA: {all_eta_text}")

        if self.queued_files:
            self.stop_downloads_btn.configure(
                state="disabled" if self.download_stop_in_progress else "normal",
                text="Stopping..." if self.download_stop_in_progress else "Stop Downloads",
            )
        else:
            self.stop_downloads_btn.configure(state="disabled", text="Stop Downloads")

    def on_history_select(self, choice: str) -> None:
        if choice and choice not in ("No History", "History"):
            self.search_entry.delete(0, "end")
            self.search_entry.insert(0, choice)
            self.save_settings()

    def save_settings(self) -> None:
        if not hasattr(self, "search_entry") or not hasattr(self, "ext_entry"):
            return
        settings = {
            "version": 1,
            "search_history": self.search_history[:10],
            "search_text": self.search_entry.get(),
            "extension_filter": self.ext_entry.get(),
            "download_directory": self.download_dir.get(),
            "sort_column": self.sort_column,
            "sort_descending": self.sort_descending,
            "filename_filter": (
                self.filename_filter_var.get()
                if hasattr(self, "filename_filter_var")
                else ""
            ),
            "hide_downloaded": (
                bool(self.hide_downloaded_var.get())
                if hasattr(self, "hide_downloaded_var")
                else False
            ),
        }
        try:
            write_app_settings(self.settings_file, settings)
        except OSError:
            LOGGER.exception("Could not save settings to %s", self.settings_file)

    def on_close(self) -> None:
        self.save_settings()
        if self.search_cancel_event is not None:
            self.search_cancel_event.set()
        self.download_cancel_event.set()
        if self.current_download_task is not None:
            self.current_download_task.cancel_event.set()
        self.end_middle_drag()
        self.destroy()


def main() -> None:
    app = PKGDownApp()
    app.mainloop()


if __name__ == "__main__":
    main()

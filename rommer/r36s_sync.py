"""Copy a local collection to an R36S ROM folder.

This is a local-file sync utility. It does not fetch files from websites.
"""

from __future__ import annotations

import os
import queue
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk


CHUNK_SIZE = 1024 * 1024
PROGRESS_CHUNK_INTERVAL = 8 * CHUNK_SIZE
TIMESTAMP_TOLERANCE_SECONDS = 2.0
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}


@dataclass(frozen=True)
class SyncItem:
    relative_path: Path
    source: Path
    destination: Path
    size: int
    current: bool


@dataclass(frozen=True)
class SyncPlan:
    source_root: Path
    destination_root: Path
    items: tuple[SyncItem, ...]
    copy_count: int
    current_count: int
    bytes_to_copy: int


class SyncCancelled(Exception):
    """Raised when the user cancels a copy operation."""


def build_plan(source_text: str, destination_text: str) -> SyncPlan:
    source_root = Path(source_text).expanduser().resolve(strict=True)
    destination_root = Path(destination_text).expanduser().resolve(strict=True)

    if not source_root.is_dir():
        raise ValueError("Choose a source folder.")
    if not destination_root.is_dir():
        raise ValueError("Choose an existing R36S ROMs folder.")
    if (
        source_root == destination_root
        or source_root in destination_root.parents
        or destination_root in source_root.parents
    ):
        raise ValueError("Source and destination must be separate folders, not nested inside one another.")

    items: list[SyncItem] = []
    for current_dir, child_dirs, file_names in os.walk(source_root, followlinks=False):
        current_path = Path(current_dir)
        child_dirs[:] = sorted(
            name for name in child_dirs if not (current_path / name).is_symlink()
        )

        for name in sorted(file_names, key=str.casefold):
            if name.casefold() in IGNORED_NAMES or name.startswith("._"):
                continue

            source_file = current_path / name
            if source_file.is_symlink() or not source_file.is_file():
                continue

            relative_path = source_file.relative_to(source_root)
            destination_file = destination_root / relative_path
            if not destination_file.resolve(strict=False).is_relative_to(destination_root):
                raise ValueError(f"Unsafe destination path: {relative_path}")

            source_stat = source_file.stat()
            destination_stat = None
            if destination_file.exists():
                if not destination_file.is_file():
                    raise ValueError(f"A folder already exists where this file should go: {relative_path}")
                destination_stat = destination_file.stat()

            current = bool(
                destination_stat
                and source_stat.st_size == destination_stat.st_size
                and abs(source_stat.st_mtime - destination_stat.st_mtime)
                <= TIMESTAMP_TOLERANCE_SECONDS
            )
            items.append(
                SyncItem(
                    relative_path=relative_path,
                    source=source_file,
                    destination=destination_file,
                    size=source_stat.st_size,
                    current=current,
                )
            )

    items.sort(key=lambda item: str(item.relative_path).casefold())
    copy_items = [item for item in items if not item.current]
    return SyncPlan(
        source_root=source_root,
        destination_root=destination_root,
        items=tuple(items),
        copy_count=len(copy_items),
        current_count=len(items) - len(copy_items),
        bytes_to_copy=sum(item.size for item in copy_items),
    )


def copy_item(
    item: SyncItem,
    cancel_event: threading.Event,
    report_progress,
) -> None:
    item.destination.parent.mkdir(parents=True, exist_ok=True)
    temp_name: str | None = None
    copied = 0
    last_reported = 0

    try:
        handle, temp_name = tempfile.mkstemp(
            prefix=".rommer-", suffix=".part", dir=item.destination.parent
        )
        os.close(handle)

        with item.source.open("rb") as source_stream, open(temp_name, "wb") as target_stream:
            while True:
                if cancel_event.is_set():
                    raise SyncCancelled
                chunk = source_stream.read(CHUNK_SIZE)
                if not chunk:
                    break
                target_stream.write(chunk)
                copied += len(chunk)
                if (
                    copied - last_reported >= PROGRESS_CHUNK_INTERVAL
                    or copied == item.size
                ):
                    report_progress(copied)
                    last_reported = copied

        shutil.copystat(item.source, temp_name)
        os.replace(temp_name, item.destination)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.remove(temp_name)
            except OSError:
                pass


class R36SSyncApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Rommer · R36S Sync")
        self.root.geometry("980x680")
        self.root.minsize(780, 520)

        self.source_var = tk.StringVar()
        self.destination_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Choose a local collection and the R36S ROMs folder, then scan.")
        self.progress_var = tk.DoubleVar(value=0)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self.plan: SyncPlan | None = None
        self.busy = False
        self.preview_limit = 500

        self._configure_style()
        self._build_ui()
        self.root.after(100, self._poll_events)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Segoe UI", 19, "bold"))
        style.configure("Subtitle.TLabel", foreground="#526174")
        style.configure("Path.TEntry", padding=7)
        style.configure("Treeview", rowheight=25)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=20)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Sync a local collection to your R36S", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "Copies existing files and keeps their folder structure. "
                "Unchanged files are skipped; files are never removed from the device."
            ),
            style="Subtitle.TLabel",
            wraplength=900,
        ).pack(anchor="w", pady=(4, 18))

        paths = ttk.LabelFrame(outer, text="Folders", padding=12)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="Local collection").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=6)
        self.source_entry = ttk.Entry(paths, textvariable=self.source_var, style="Path.TEntry")
        self.source_entry.grid(row=0, column=1, sticky="ew", pady=6)
        ttk.Button(paths, text="Browse…", command=self._choose_source).grid(row=0, column=2, padx=(8, 0), pady=6)

        ttk.Label(paths, text="R36S ROMs folder").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=6)
        self.destination_entry = ttk.Entry(paths, textvariable=self.destination_var, style="Path.TEntry")
        self.destination_entry.grid(row=1, column=1, sticky="ew", pady=6)
        ttk.Button(paths, text="Browse…", command=self._choose_destination).grid(row=1, column=2, padx=(8, 0), pady=6)
        ttk.Label(
            paths,
            text="Select the EASYROMS partition root on the SD card, or the /roms folder on a network share.",
            style="Subtitle.TLabel",
        ).grid(row=2, column=1, sticky="w", pady=(0, 4))

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(14, 10))
        self.scan_button = ttk.Button(actions, text="Scan and preview", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.sync_button = ttk.Button(actions, text="Sync changed files", command=self._start_sync, state="disabled")
        self.sync_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=(8, 0))

        self.summary_label = ttk.Label(actions, text="")
        self.summary_label.pack(side="right")

        preview = ttk.LabelFrame(outer, text="Preview", padding=8)
        preview.pack(fill="both", expand=True)
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(
            preview,
            columns=("action", "size"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="Relative path")
        self.tree.heading("action", text="Action")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=620, minwidth=240, stretch=True)
        self.tree.column("action", width=120, minwidth=100, stretch=False, anchor="center")
        self.tree.column("size", width=120, minwidth=100, stretch=False, anchor="e")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(preview, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x", pady=(12, 4))
        self.progress = ttk.Progressbar(progress_row, variable=self.progress_var, maximum=100)
        self.progress.pack(fill="x", expand=True)
        self.status_label = ttk.Label(outer, textvariable=self.status_var, anchor="w")
        self.status_label.pack(fill="x")

    def _choose_source(self) -> None:
        selected = filedialog.askdirectory(title="Choose your local collection")
        if selected:
            self.source_var.set(selected)

    def _choose_destination(self) -> None:
        selected = filedialog.askdirectory(title="Choose the R36S ROMs folder")
        if selected:
            self.destination_var.set(selected)

    def _set_busy(self, busy: bool, task: str = "") -> None:
        self.busy = busy
        self.scan_button.configure(state="disabled" if busy else "normal")
        self.sync_button.configure(
            state="normal" if (not busy and self.plan and self.plan.copy_count > 0) else "disabled"
        )
        self.cancel_button.configure(state="normal" if busy else "disabled")
        if busy:
            self.status_var.set(task)

    def _start_scan(self) -> None:
        if self.busy:
            return
        self.plan = None
        self.sync_button.configure(state="disabled")
        self.summary_label.configure(text="")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._set_busy(True, "Scanning local files…")
        source_text = self.source_var.get().strip()
        destination_text = self.destination_var.get().strip()

        def worker() -> None:
            try:
                plan = build_plan(source_text, destination_text)
            except Exception as exc:  # Report filesystem and path errors in the UI.
                self.events.put(("scan_error", str(exc)))
            else:
                self.events.put(("scan_done", plan))

        threading.Thread(target=worker, daemon=True).start()

    def _start_sync(self) -> None:
        if self.busy or not self.plan:
            return

        try:
            same_source = Path(self.source_var.get()).expanduser().resolve() == self.plan.source_root
            same_destination = Path(self.destination_var.get()).expanduser().resolve() == self.plan.destination_root
        except OSError:
            same_source = same_destination = False
        if not (same_source and same_destination):
            messagebox.showinfo("Scan again", "The selected folders changed. Scan again to refresh the preview.")
            self._start_scan()
            return

        assert self.plan is not None
        size_text = self._format_size(self.plan.bytes_to_copy)
        confirmed = messagebox.askyesno(
            "Confirm sync",
            f"Copy {self.plan.copy_count:,} changed files ({size_text}) to:\n\n"
            f"{self.plan.destination_root}\n\n"
            "Files with different size or modified time will be replaced. No device files will be deleted.",
        )
        if not confirmed:
            return

        plan = self.plan
        self.cancel_event.clear()
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(1, plan.bytes_to_copy))
        self.progress_var.set(0)
        self._set_busy(True, "Starting sync…")

        def worker() -> None:
            copied_count = 0
            current_count = 0
            bytes_done = 0
            try:
                for item in plan.items:
                    if item.current:
                        current_count += 1
                        continue
                    if self.cancel_event.is_set():
                        raise SyncCancelled

                    self.events.put(("file_start", str(item.relative_path)))

                    def report_file_progress(file_bytes: int) -> None:
                        self.events.put(("progress", bytes_done + file_bytes))

                    copy_item(item, self.cancel_event, report_file_progress)
                    copied_count += 1
                    bytes_done += item.size
                    self.events.put(("file_done", str(item.relative_path), bytes_done))

                self.events.put(("sync_done", copied_count, current_count, bytes_done))
            except SyncCancelled:
                self.events.put(("sync_cancelled", copied_count, current_count, bytes_done))
            except Exception as exc:
                self.events.put(("sync_error", str(exc), copied_count, bytes_done))

        threading.Thread(target=worker, daemon=True).start()

    def _cancel(self) -> None:
        if self.busy:
            self.cancel_event.set()
            self.status_var.set("Cancelling after the current file…")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "scan_error":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100)
                    self.progress_var.set(0)
                    self._set_busy(False)
                    self.status_var.set("Scan failed.")
                    messagebox.showerror("Could not scan", event[1])
                elif kind == "scan_done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100)
                    self.progress_var.set(0)
                    self.plan = event[1]
                    self._show_plan(self.plan)
                    self._set_busy(False)
                    self.summary_label.configure(
                        text=(
                            f"{self.plan.copy_count:,} to copy · "
                            f"{self.plan.current_count:,} already current · "
                            f"{self._format_size(self.plan.bytes_to_copy)}"
                        )
                    )
                    if self.plan.copy_count:
                        self.status_var.set("Preview ready. Review the plan, then choose Sync changed files.")
                    elif not self.plan.items:
                        self.status_var.set("No files found under the selected source folder.")
                    else:
                        self.status_var.set("Everything in the source folder is already current on the device.")
                elif kind == "file_start":
                    self.status_var.set(f"Copying {event[1]}…")
                elif kind == "progress":
                    self.progress_var.set(event[1])
                elif kind == "file_done":
                    self.progress_var.set(event[2])
                elif kind == "sync_done":
                    self.progress_var.set(self.plan.bytes_to_copy if self.plan else 0)
                    self._set_busy(False)
                    self.status_var.set(
                        f"Sync complete: copied {event[1]:,} files; {event[2]:,} files were already current."
                    )
                    messagebox.showinfo("Sync complete", self.status_var.get())
                    self._start_scan()
                elif kind == "sync_cancelled":
                    self.progress_var.set(event[3])
                    self._set_busy(False)
                    self.status_var.set(f"Sync cancelled after {event[1]:,} files. Scanning the current state…")
                    self._start_scan()
                elif kind == "sync_error":
                    self.progress_var.set(event[3])
                    self._set_busy(False)
                    self.status_var.set(f"Sync stopped after {event[2]:,} files.")
                    messagebox.showerror("Sync stopped", event[1])
                    self._start_scan()
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _show_plan(self, plan: SyncPlan) -> None:
        self.tree.delete(*self.tree.get_children())
        for item in plan.items[: self.preview_limit]:
            action = "Already current" if item.current else "Copy"
            self.tree.insert(
                "",
                "end",
                text=str(item.relative_path),
                values=(action, self._format_size(item.size)),
            )
        if len(plan.items) > self.preview_limit:
            self.tree.insert(
                "",
                "end",
                text=f"… {len(plan.items) - self.preview_limit:,} more files (included in sync)",
                values=("Preview limited", ""),
            )

    @staticmethod
    def _format_size(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
            value /= 1024
        return f"{size} B"


def main() -> None:
    root = tk.Tk()
    R36SSyncApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

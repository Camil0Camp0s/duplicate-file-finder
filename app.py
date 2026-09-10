"""Duplicate File Finder & Cleaner — Tkinter desktop app.

Lets a non-technical user pick a folder, scan it for byte-identical
duplicate files, review the results (with an image preview when
applicable), and send the unwanted copies to the Recycle Bin — never a
hard delete, so a mistake is always recoverable.

The scan runs on a background thread (duplicate_finder.scan_for_duplicates
is pure filesystem/hashlib work) so the Tkinter main loop never freezes on
large folders; progress messages come back through a thread-safe queue
polled with `after()`, which is the standard safe pattern for updating
Tkinter widgets from another thread.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from send2trash import send2trash

from duplicate_finder import DuplicateGroup, FileInfo, scan_for_duplicates

try:
    from PIL import Image, ImageTk

    PIL_AVAILABLE = True
except ImportError:  # Pillow is a dependency (see requirements.txt) but degrade gracefully if missing
    PIL_AVAILABLE = False

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
THUMBNAIL_SIZE = (240, 240)
KEEP = "KEEP"
DUPLICATE = "DUPLICATE"


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


class DuplicateFinderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Duplicate File Finder & Cleaner")
        self.geometry("1080x640+80+40")
        self.minsize(760, 480)

        self.selected_folder: Path | None = None
        self.groups: list[DuplicateGroup] = []
        # Treeview item id -> FileInfo, so a click can look up what a row represents
        self.row_to_file: dict[str, FileInfo] = {}
        self.row_to_status: dict[str, str] = {}
        self.progress_queue: queue.Queue[str] = queue.Queue()
        self._thumbnail_ref: ImageTk.PhotoImage | None = None  # must keep a reference or Tkinter garbage-collects it

        self._build_layout()

    # ---------- UI construction ----------

    def _build_layout(self) -> None:
        # Top-level layout uses grid (not pack) with row 2 (body) as the only row that
        # gets weight — this pins the status/hint/bottom bars to their natural height and
        # guarantees they stay visible regardless of how much vertical space the Treeview wants.
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=10)
        top.grid(row=0, column=0, sticky="ew")

        self.folder_var = tk.StringVar(value="No folder selected")
        ttk.Label(top, textvariable=self.folder_var, foreground="#444").pack(side="left", fill="x", expand=True)
        ttk.Button(top, text="Choose Folder...", command=self.choose_folder).pack(side="right", padx=(6, 0))
        self.scan_button = ttk.Button(top, text="Scan for Duplicates", command=self.start_scan, state="disabled")
        self.scan_button.pack(side="right")

        self.status_var = tk.StringVar(value="Choose a folder to begin.")
        ttk.Label(self, textvariable=self.status_var, padding=(10, 0)).grid(row=1, column=0, sticky="ew")

        body = ttk.Frame(self, padding=(10, 6))
        body.grid(row=2, column=0, sticky="nsew")

        hint = ttk.Label(
            self,
            text='Double-click a row to toggle KEEP / DUPLICATE. Each group always keeps at least one file.',
            foreground="#666",
            padding=(10, 2),
        )
        hint.grid(row=3, column=0, sticky="ew")

        bottom = ttk.Frame(self, padding=10)
        bottom.grid(row=4, column=0, sticky="ew")
        self.summary_var = tk.StringVar(value="")
        ttk.Label(bottom, textvariable=self.summary_var).pack(side="left")
        self.quarantine_button = ttk.Button(
            bottom, text="Move Marked Duplicates to Recycle Bin", command=self.remove_duplicates, state="disabled"
        )
        self.quarantine_button.pack(side="right")

        columns = ("group", "path", "size", "status")
        self.tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("group", text="Group")
        self.tree.heading("path", text="File Path")
        self.tree.heading("size", text="Size")
        self.tree.heading("status", text="Status")
        self.tree.column("group", width=70, anchor="center")
        self.tree.column("path", width=560)
        self.tree.column("size", width=90, anchor="e")
        self.tree.column("status", width=100, anchor="center")
        self.tree.tag_configure(KEEP, foreground="#1a7f37")
        self.tree.tag_configure(DUPLICATE, foreground="#c0392b")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)

        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="left", fill="y")

        preview = ttk.Frame(body, padding=(10, 0), width=260)
        preview.pack(side="right", fill="y")
        preview.pack_propagate(False)
        ttk.Label(preview, text="Preview", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.preview_image_label = ttk.Label(preview)
        self.preview_image_label.pack(pady=8)
        self.preview_text = tk.Text(preview, height=10, width=32, wrap="word", state="disabled", bg=self.cget("bg"), relief="flat")
        self.preview_text.pack(fill="x")

    # ---------- folder selection & scan ----------

    def choose_folder(self) -> None:
        folder = filedialog.askdirectory(title="Choose a folder to scan for duplicates")
        if not folder:
            return
        self.selected_folder = Path(folder)
        self.folder_var.set(str(self.selected_folder))
        self.scan_button.config(state="normal")
        self.status_var.set("Ready to scan.")

    def start_scan(self) -> None:
        if not self.selected_folder:
            return
        self.scan_button.config(state="disabled")
        self.quarantine_button.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.row_to_file.clear()
        self.row_to_status.clear()
        self.summary_var.set("")
        self.status_var.set("Scanning...")

        thread = threading.Thread(target=self._scan_worker, daemon=True)
        thread.start()
        self.after(100, self._poll_progress)

    def _scan_worker(self) -> None:
        groups = scan_for_duplicates(self.selected_folder, progress_callback=self.progress_queue.put)
        self.groups = groups
        self.progress_queue.put("__DONE__")

    def _poll_progress(self) -> None:
        try:
            while True:
                message = self.progress_queue.get_nowait()
                if message == "__DONE__":
                    self._populate_results()
                    return
                self.status_var.set(message)
        except queue.Empty:
            pass
        self.after(100, self._poll_progress)

    # ---------- results ----------

    def _populate_results(self) -> None:
        self.scan_button.config(state="normal")
        total_files = sum(len(g.files) for g in self.groups)
        wasted = sum(g.wasted_bytes for g in self.groups)

        if not self.groups:
            self.status_var.set("No duplicate files found.")
            self.summary_var.set("")
            return

        for group_index, group in enumerate(self.groups, start=1):
            for file_index, info in enumerate(group.files):
                status = KEEP if file_index == 0 else DUPLICATE  # oldest file (index 0) is kept by default
                try:
                    display_path = str(info.path.relative_to(self.selected_folder))
                except ValueError:
                    display_path = str(info.path)
                row_id = self.tree.insert(
                    "",
                    "end",
                    values=(group_index, display_path, format_size(info.size), status),
                    tags=(status,),
                )
                self.row_to_file[row_id] = info
                self.row_to_status[row_id] = status

        self.status_var.set(f"Found {len(self.groups)} duplicate group(s) across {total_files} files.")
        self.summary_var.set(f"{total_files - len(self.groups)} duplicate file(s) marked — {format_size(wasted)} can be freed.")
        self.quarantine_button.config(state="normal")

    def _on_double_click(self, _event: tk.Event) -> None:
        row_id = self.tree.focus()
        if not row_id:
            return
        group_index = int(self.tree.set(row_id, "group"))
        current_status = self.row_to_status[row_id]

        if current_status == KEEP:
            new_status = DUPLICATE
        else:
            # Refuse to leave a group with zero KEEP files — that would quarantine every copy.
            siblings = [r for r in self.row_to_file if int(self.tree.set(r, "group")) == group_index]
            other_keeps = [r for r in siblings if r != row_id and self.row_to_status[r] == KEEP]
            if not other_keeps:
                messagebox.showinfo("Keep at least one copy", "Mark another file in this group as KEEP first.")
                return
            new_status = KEEP

        self.row_to_status[row_id] = new_status
        self.tree.item(row_id, tags=(new_status,))
        self.tree.set(row_id, "status", new_status)
        self._refresh_summary()

    def _refresh_summary(self) -> None:
        duplicate_rows = [r for r, s in self.row_to_status.items() if s == DUPLICATE]
        wasted = sum(self.row_to_file[r].size for r in duplicate_rows)
        self.summary_var.set(f"{len(duplicate_rows)} duplicate file(s) marked — {format_size(wasted)} can be freed.")

    def _on_select(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        info = self.row_to_file.get(selection[0])
        if not info:
            return
        self._show_preview(info)

    def _show_preview(self, info: FileInfo) -> None:
        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert(
            "end", f"{info.path.name}\n\nSize: {format_size(info.size)}\nFolder: {info.path.parent}"
        )
        self.preview_text.config(state="disabled")

        if PIL_AVAILABLE and info.path.suffix.lower() in IMAGE_EXTENSIONS and info.path.exists():
            try:
                image = Image.open(info.path)
                image.thumbnail(THUMBNAIL_SIZE)
                self._thumbnail_ref = ImageTk.PhotoImage(image)
                self.preview_image_label.config(image=self._thumbnail_ref, text="")
                return
            except Exception:
                pass  # corrupt/unreadable image — fall through to the text-only preview
        self.preview_image_label.config(image="", text="(no preview)")
        self._thumbnail_ref = None

    # ---------- cleanup action ----------

    def remove_duplicates(self) -> None:
        duplicate_rows = [r for r, s in self.row_to_status.items() if s == DUPLICATE]
        if not duplicate_rows:
            messagebox.showinfo("Nothing to do", "No files are marked as DUPLICATE.")
            return

        count = len(duplicate_rows)
        wasted = sum(self.row_to_file[r].size for r in duplicate_rows)
        if not messagebox.askyesno(
            "Confirm",
            f"Send {count} duplicate file(s) to the Recycle Bin?\nThis will free about {format_size(wasted)}.\n\n"
            "Files go to the Recycle Bin, not permanent deletion — you can restore them from there.",
        ):
            return

        failed: list[str] = []
        for row_id in duplicate_rows:
            info = self.row_to_file[row_id]
            try:
                send2trash(str(info.path))
                self.tree.delete(row_id)
                del self.row_to_file[row_id]
                del self.row_to_status[row_id]
            except OSError as exc:
                failed.append(f"{info.path.name}: {exc}")

        self._refresh_summary()
        if failed:
            messagebox.showwarning("Some files could not be removed", "\n".join(failed))
        else:
            self.status_var.set(f"Moved {count} duplicate file(s) to the Recycle Bin.")


if __name__ == "__main__":
    app = DuplicateFinderApp()
    app.mainloop()

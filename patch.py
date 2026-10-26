import os

def patch_main():
    with open('main.py', 'r') as f:
        content = f.read()
        
    # Replace the top imports to include queue and time
    import_replacement = """import customtkinter as ctk
import os
import json
import time
import threading
import queue
from tkinter import filedialog"""
    content = content.replace("import customtkinter as ctk\nimport os\nimport json\nfrom tkinter import filedialog", import_replacement)
    
    # Init variables
    init_replacement = """        self.download_dir = ctk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.search_history = []
        
        # Download Queue
        self.download_queue = queue.Queue()
        self.queued_files = 0
        self.queued_bytes = 0
        self.current_downloading = ""
        self.current_speed = 0.0
        
        self.load_history()
        
        self.build_ui()
        
        threading.Thread(target=self.download_worker, daemon=True).start()"""
    content = content.replace('        self.download_dir = ctk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))\n        self.search_history = []\n        \n        self.load_history()\n        \n        self.build_ui()', init_replacement)

    # UI updates: add ext filter and bottom status bar
    ui_replacement = """        self.search_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Search Archive.org...", width=300)
        self.search_entry.pack(side="left", padx=10)
        
        self.ext_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Ext (e.g. pkg)", width=100)
        self.ext_entry.pack(side="left", padx=5)
        
        self.search_btn = ctk.CTkButton(self.top_frame, text="Search", command=self.on_search)
        self.search_btn.pack(side="left", padx=5)"""
    content = content.replace('        self.search_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Search Archive.org...", width=400)\n        self.search_entry.pack(side="left", padx=10)\n        \n        self.search_btn = ctk.CTkButton(self.top_frame, text="Search", command=self.on_search)\n        self.search_btn.pack(side="left", padx=5)', ui_replacement)

    bottom_bar_add = """        # Scrollable frame for results
        self.results_frame = ctk.CTkScrollableFrame(self)
        self.results_frame.pack(padx=10, pady=10, fill="both", expand=True)
        
        # Bottom status bar
        self.bottom_frame = ctk.CTkFrame(self, height=30)
        self.bottom_frame.pack(side="bottom", fill="x", padx=10, pady=(0,10))
        self.status_file_label = ctk.CTkLabel(self.bottom_frame, text="Idle", width=300, anchor="w")
        self.status_file_label.pack(side="left", padx=10)
        self.status_speed_label = ctk.CTkLabel(self.bottom_frame, text="0 KB/s", width=100, anchor="w")
        self.status_speed_label.pack(side="left", padx=10)
        self.status_queued_label = ctk.CTkLabel(self.bottom_frame, text="Queued: 0", width=100, anchor="w")
        self.status_queued_label.pack(side="left", padx=10)
        self.status_size_label = ctk.CTkLabel(self.bottom_frame, text="Space: 0 MB", width=150, anchor="w")
        self.status_size_label.pack(side="left", padx=10)"""
    content = content.replace('        # Scrollable frame for results\n        self.results_frame = ctk.CTkScrollableFrame(self)\n        self.results_frame.pack(padx=10, pady=10, fill="both", expand=True)', bottom_bar_add)

    # Search filter logic
    search_logic = """    def perform_search(self, query):
        import requests
        import datetime
        ext_filter = self.ext_entry.get().strip().lower()
        if ext_filter and not ext_filter.startswith('.'):
            ext_filter = '.' + ext_filter"""
    content = content.replace('    def perform_search(self, query):\n        import requests\n        import datetime', search_logic)
    
    filter_logic = """                    # Filter out metadata files if needed, here just basic files
                    fname = f.get("name")
                    if not fname: continue
                    if ext_filter and not fname.lower().endswith(ext_filter):
                        continue"""
    content = content.replace('                    # Filter out metadata files if needed, here just basic files\n                    fname = f.get("name")\n                    if not fname: continue', filter_logic)
    
    # UI styling in add_file_to_ui
    style_logic = """    def add_file_to_ui(self, identifier, fname, fsize, upload_date, download_url):
        idx = len(self.results_frame.winfo_children()) + 1
        
        # Frame for each item
        item_frame = ctk.CTkFrame(self.results_frame)
        item_frame.pack(fill="x", padx=5, pady=5)
        
        # Info frame
        info_frame = ctk.CTkFrame(item_frame, fg_color="transparent")
        info_frame.pack(side="left", fill="x", expand=True)
        
        idx_lbl = ctk.CTkLabel(info_frame, text=f"[{idx}] - ", text_color="#cccccc")
        idx_lbl.pack(side="left")
        
        name_part, ext_part = os.path.splitext(fname)
        
        name_lbl = ctk.CTkLabel(info_frame, text=name_part, text_color="#ffffff")
        name_lbl.pack(side="left")
        
        ext_lbl = ctk.CTkLabel(info_frame, text=ext_part, text_color="#4aa1ff")
        ext_lbl.pack(side="left")
        
        details_lbl = ctk.CTkLabel(info_frame, text=f" - {fsize} bytes - {upload_date}", text_color="#cccccc")
        details_lbl.pack(side="left")"""
    content = content.replace('    def add_file_to_ui(self, identifier, fname, fsize, upload_date, download_url):\n        idx = len(self.results_frame.winfo_children()) + 1\n        \n        # Frame for each item\n        item_frame = ctk.CTkFrame(self.results_frame)\n        item_frame.pack(fill="x", padx=5, pady=5)\n        \n        # Info label\n        info_text = f"[{idx}] - {fname} - {fsize} bytes - {upload_date}"\n        info_label = ctk.CTkLabel(item_frame, text=info_text, anchor="w")\n        info_label.pack(side="left", padx=10, fill="x", expand=True)', style_logic)
    
    # Replace start_download and download_thread
    new_dl_logic = """        # Wire commands
        download_btn.configure(command=lambda: self.queue_download(download_url, fname, int(fsize) if str(fsize).isdigit() else 0, progress_bar, status_label, download_btn, delete_btn))
        delete_btn.configure(command=lambda: self.delete_file(fname, status_label, download_btn, delete_btn, progress_bar))
        
        # Check if already downloaded
        dest_path = os.path.join(self.download_dir.get(), fname)
        if os.path.exists(dest_path):
            status_label.configure(text="Downloaded")
            progress_bar.set(1)
            delete_btn.pack(side="right", padx=5)
        else:
            download_btn.pack(side="right", padx=5)

    def queue_download(self, url, fname, size_bytes, progress_bar, status_label, download_btn, delete_btn):
        self.download_queue.put((url, fname, size_bytes, progress_bar, status_label, download_btn, delete_btn))
        self.queued_files += 1
        self.queued_bytes += size_bytes
        self.update_bottom_bar()
        download_btn.configure(state="disabled", text="Queued")
        status_label.configure(text="Queued")

    def update_bottom_bar(self):
        self.status_file_label.configure(text=f"Downloading: {self.current_downloading}" if self.current_downloading else "Idle")
        self.status_speed_label.configure(text=f"{self.current_speed:.1f} KB/s")
        self.status_queued_label.configure(text=f"Queued: {self.queued_files}")
        self.status_size_label.configure(text=f"Space: {self.queued_bytes / (1024*1024):.1f} MB")

    def download_worker(self):
        import requests
        while True:
            url, fname, size_bytes, progress_bar, status_label, download_btn, delete_btn = self.download_queue.get()
            self.current_downloading = fname
            self.current_speed = 0.0
            
            dest_path = os.path.join(self.download_dir.get(), fname)
            
            try:
                self.after(0, lambda pb=progress_bar, sl=status_label: [
                    sl.configure(text="Downloading..."),
                    pb.set(0)
                ])
                self.after(0, self.update_bottom_bar)
                
                with requests.get(url, stream=True, timeout=10) as r:
                    r.raise_for_status()
                    total_length = int(r.headers.get('content-length', size_bytes))
                    downloaded = 0
                    start_time = time.time()
                    
                    with open(dest_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)
                                
                                now = time.time()
                                dt = now - start_time
                                if dt > 0.5:
                                    self.current_speed = (downloaded / 1024) / dt
                                    if total_length > 0:
                                        progress = downloaded / total_length
                                        self.after(0, progress_bar.set, progress)
                                    self.after(0, self.update_bottom_bar)
                
                self.after(0, lambda sl=status_label, db=download_btn, delb=delete_btn, pb=progress_bar: [
                    sl.configure(text="Downloaded"),
                    pb.set(1),
                    db.pack_forget(),
                    delb.pack(side="right", padx=5)
                ])
                
            except Exception as e:
                print("Download error:", e)
                self.after(0, lambda sl=status_label, db=download_btn: [
                    sl.configure(text="Error"),
                    db.configure(state="normal", text="Download")
                ])
                
            self.queued_files -= 1
            self.queued_bytes -= size_bytes
            self.current_downloading = ""
            self.current_speed = 0.0
            self.after(0, self.update_bottom_bar)
            self.download_queue.task_done()"""
            
    # Cut out the old start_download and _download_thread
    old_start_dl = """        # Wire commands
        download_btn.configure(command=lambda: self.start_download(download_url, fname, progress_bar, status_label, download_btn, delete_btn))
        delete_btn.configure(command=lambda: self.delete_file(fname, status_label, download_btn, delete_btn, progress_bar))
        
        # Check if already downloaded
        dest_path = os.path.join(self.download_dir.get(), fname)
        if os.path.exists(dest_path):
            status_label.configure(text="Downloaded")
            progress_bar.set(1)
            delete_btn.pack(side="right", padx=5)
        else:
            download_btn.pack(side="right", padx=5)

    def start_download(self, url, fname, progress_bar, status_label, download_btn, delete_btn):
        import threading
        threading.Thread(target=self._download_thread, args=(url, fname, progress_bar, status_label, download_btn, delete_btn), daemon=True).start()

    def _download_thread(self, url, fname, progress_bar, status_label, download_btn, delete_btn):
        import requests
        dest_path = os.path.join(self.download_dir.get(), fname)
        
        try:
            self.after(0, lambda: [
                download_btn.configure(state="disabled"),
                status_label.configure(text="Downloading..."),
                progress_bar.set(0)
            ])
            
            with requests.get(url, stream=True, timeout=10) as r:
                r.raise_for_status()
                total_length = int(r.headers.get('content-length', 0))
                downloaded = 0
                
                with open(dest_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total_length > 0:
                                progress = downloaded / total_length
                                self.after(0, progress_bar.set, progress)
            
            self.after(0, lambda: [
                status_label.configure(text="Downloaded"),
                download_btn.pack_forget(),
                delete_btn.pack(side="right", padx=5)
            ])
            
        except Exception as e:
            print("Download error:", e)
            self.after(0, lambda: [
                status_label.configure(text="Error"),
                download_btn.configure(state="normal")
            ])"""
    
    content = content.replace(old_start_dl, new_dl_logic)
    
    with open('main.py', 'w') as f:
        f.write(content)

patch_main()

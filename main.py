import customtkinter as ctk
import os
import json
import time
import threading
import queue
from tkinter import filedialog

CONFIG_FILE = "search_history.json"

class PKGDownApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")
        
        self.geometry("900x700")
        self.title("PKG_D0Wn")
        
        self.download_dir = ctk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self.search_history = []
        
        # Download Queue
        self.download_queue = queue.Queue()
        self.queued_files = 0
        self.queued_bytes = 0
        self.current_downloading = ""
        self.current_speed = 0.0
        
        self.load_history()
        
        self.build_ui()
        
        threading.Thread(target=self.download_worker, daemon=True).start()
        
    def build_ui(self):
        # Top frame for search and browse
        self.top_frame = ctk.CTkFrame(self)
        self.top_frame.pack(padx=10, pady=10, fill="x")
        
        self.search_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Search Archive.org...", width=300)
        self.search_entry.pack(side="left", padx=10)
        
        self.ext_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Ext (e.g. pkg)", width=100)
        self.ext_entry.pack(side="left", padx=5)
        
        self.search_btn = ctk.CTkButton(self.top_frame, text="Search", command=self.on_search)
        self.search_btn.pack(side="left", padx=5)
        
        self.history_opt = ctk.CTkOptionMenu(self.top_frame, values=self.search_history if self.search_history else ["No History"], command=self.on_history_select)
        self.history_opt.pack(side="left", padx=5)
        
        self.dir_label = ctk.CTkLabel(self.top_frame, textvariable=self.download_dir, width=200, anchor="w")
        self.dir_label.pack(side="right", padx=10)
        
        self.browse_btn = ctk.CTkButton(self.top_frame, text="Browse", command=self.browse_dir, width=80)
        self.browse_btn.pack(side="right", padx=5)
        
        # Scrollable frame for results
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
        self.status_size_label.pack(side="left", padx=10)
        
    def browse_dir(self):
        directory = filedialog.askdirectory(initialdir=self.download_dir.get())
        if directory:
            self.download_dir.set(directory)
            
    def on_search(self):
        query = self.search_entry.get().strip()
        if query:
            if query not in self.search_history:
                self.search_history.insert(0, query)
                self.search_history = self.search_history[:10]
                self.history_opt.configure(values=self.search_history)
                self.save_history()
            print(f"Searching for: {query}")
            
            # Clear previous results
            for widget in self.results_frame.winfo_children():
                widget.destroy()
                
            self.search_btn.configure(state="disabled")
            
            # Start background thread
            import threading
            threading.Thread(target=self.perform_search, args=(query,), daemon=True).start()

    def perform_search(self, query):
        import requests
        import datetime
        ext_filter = self.ext_entry.get().strip().lower()
        if ext_filter and not ext_filter.startswith('.'):
            ext_filter = '.' + ext_filter
        try:
            # 1. Search for items (Fetch up to 1000 items to get many more files)
            url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=1000"
            response = requests.get(url, timeout=10)
            data = response.json()
            docs = data.get("response", {}).get("docs", [])
            
            for doc in docs:
                identifier = doc.get("identifier")
                if not identifier:
                    continue
                
                # 2. Fetch item metadata for files
                meta_url = f"https://archive.org/metadata/{identifier}"
                meta_resp = requests.get(meta_url, timeout=10)
                meta_data = meta_resp.json()
                
                files = meta_data.get("files", [])
                server = meta_data.get("server")
                dir_path = meta_data.get("dir")
                
                for f in files:
                    # Filter out metadata files if needed, here just basic files
                    fname = f.get("name")
                    if not fname: continue
                    if ext_filter and not fname.lower().endswith(ext_filter):
                        continue
                    fsize = f.get("size", "0")
                    fmtime = f.get("mtime", "")
                    if fmtime:
                        try:
                            # Convert epoch to readable date
                            upload_date = datetime.datetime.fromtimestamp(int(fmtime)).strftime('%Y-%m-%d %H:%M')
                        except:
                            upload_date = "Unknown"
                    else:
                        upload_date = "Unknown"
                    
                    download_url = f"https://{server}{dir_path}/{fname}"
                    
                    # Send to UI
                    self.after(0, self.add_file_to_ui, identifier, fname, fsize, upload_date, download_url)
        except Exception as e:
            print("Search error:", e)
        finally:
            self.after(0, lambda: self.search_btn.configure(state="normal"))

    def add_file_to_ui(self, identifier, fname, fsize, upload_date, download_url):
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
        details_lbl.pack(side="left")
        
        # Progress bar
        progress_bar = ctk.CTkProgressBar(item_frame, width=150)
        progress_bar.set(0)
        progress_bar.pack(side="left", padx=10)
        
        # Status label
        status_label = ctk.CTkLabel(item_frame, text="", width=80)
        status_label.pack(side="left", padx=5)
        
        # Buttons
        download_btn = ctk.CTkButton(item_frame, text="Download", width=80)
        delete_btn = ctk.CTkButton(item_frame, text="Delete", width=80, fg_color="red", hover_color="darkred")
        
        # Wire commands
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
            self.download_queue.task_done()

    def delete_file(self, fname, status_label, download_btn, delete_btn, progress_bar):
        dest_path = os.path.join(self.download_dir.get(), fname)
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except Exception as e:
                print("Delete error:", e)
                return
        
        status_label.configure(text="")
        progress_bar.set(0)
        delete_btn.pack_forget()
        download_btn.configure(state="normal")
        download_btn.pack(side="right", padx=5)
        
    def on_history_select(self, choice):
        if choice and choice != "No History":
            self.search_entry.delete(0, 'end')
            self.search_entry.insert(0, choice)

    def load_history(self):
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    self.search_history = json.load(f)
            except:
                pass

    def save_history(self):
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.search_history, f)
        except:
            pass

def main():
    app = PKGDownApp()
    app.mainloop()

if __name__ == "__main__":
    main()

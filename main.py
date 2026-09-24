import customtkinter as ctk
import os
import json
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
        
        self.load_history()
        
        self.build_ui()
        
    def build_ui(self):
        # Top frame for search and browse
        self.top_frame = ctk.CTkFrame(self)
        self.top_frame.pack(padx=10, pady=10, fill="x")
        
        self.search_entry = ctk.CTkEntry(self.top_frame, placeholder_text="Search Archive.org...", width=400)
        self.search_entry.pack(side="left", padx=10)
        
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
        try:
            # 1. Search for items
            url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=5"
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
        # We will build the full UI item in Task 5
        pass

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
            ])

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

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
            # To be implemented: Archive.org API logic
            
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

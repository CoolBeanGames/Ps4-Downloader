import os

def patch_main():
    with open('main.py', 'r') as f:
        content = f.read()

    # 1. Add current_page and current_query
    init_old = """        self.search_history = []
        
        # Download Queue"""
    init_new = """        self.search_history = []
        self.current_page = 1
        self.current_query = ""
        self.next_page_btn = None
        
        # Download Queue"""
    content = content.replace(init_old, init_new)

    # 2. Update on_search
    on_search_old = """    def on_search(self):
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
            threading.Thread(target=self.perform_search, args=(query,), daemon=True).start()"""
    on_search_new = """    def on_search(self):
        query = self.search_entry.get().strip()
        if query:
            if query not in self.search_history:
                self.search_history.insert(0, query)
                self.search_history = self.search_history[:10]
                self.history_opt.configure(values=self.search_history)
                self.save_history()
            print(f"Searching for: {query}")
            
            self.current_page = 1
            self.current_query = query
            
            # Clear previous results
            for widget in self.results_frame.winfo_children():
                widget.destroy()
            self.next_page_btn = None
                
            self.search_btn.configure(state="disabled")
            
            # Start background thread
            import threading
            threading.Thread(target=self.perform_search, args=(query, 1), daemon=True).start()"""
    content = content.replace(on_search_old, on_search_new)

    # 3. Update perform_search
    perf_search_old = """    def perform_search(self, query):
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
            docs = data.get("response", {}).get("docs", [])"""
    perf_search_new = """    def perform_search(self, query, page=1):
        import requests
        import datetime
        ext_filter = self.ext_entry.get().strip().lower()
        if ext_filter and not ext_filter.startswith('.'):
            ext_filter = '.' + ext_filter
        try:
            # 1. Search for items (Fetch up to 1000 items to get many more files)
            url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=1000&page={page}"
            response = requests.get(url, timeout=10)
            data = response.json()
            docs = data.get("response", {}).get("docs", [])"""
    content = content.replace(perf_search_old, perf_search_new)

    # 4. Handle next page logic at end of perform_search
    end_search_old = """        except Exception as e:
            print("Search error:", e)
        finally:
            self.after(0, lambda: self.search_btn.configure(state="normal"))"""
    end_search_new = """        except Exception as e:
            print("Search error:", e)
        finally:
            self.after(0, lambda: self.search_btn.configure(state="normal"))
            if 'docs' in locals() and len(docs) == 1000:
                self.after(0, self.add_next_page_button)

    def add_next_page_button(self):
        if self.next_page_btn is not None and self.next_page_btn.winfo_exists():
            self.next_page_btn.destroy()
            
        self.next_page_btn = ctk.CTkButton(self.results_frame, text="Load Next 1000 Results", command=self.load_next_page)
        self.next_page_btn.pack(pady=20)
        
    def load_next_page(self):
        self.current_page += 1
        self.next_page_btn.configure(text="Loading...", state="disabled")
        import threading
        threading.Thread(target=self.perform_search, args=(self.current_query, self.current_page), daemon=True).start()"""
    content = content.replace(end_search_old, end_search_new)

    # 5. Fix add_file_to_ui placing the file frame correctly (must be placed before the button if the button exists)
    add_file_old = """    def add_file_to_ui(self, identifier, fname, fsize, upload_date, download_url):
        idx = len(self.results_frame.winfo_children()) + 1
        
        # Frame for each item
        item_frame = ctk.CTkFrame(self.results_frame)
        item_frame.pack(fill="x", padx=5, pady=5)"""
    add_file_new = """    def add_file_to_ui(self, identifier, fname, fsize, upload_date, download_url):
        idx = len(self.results_frame.winfo_children()) + 1
        if self.next_page_btn is not None and self.next_page_btn.winfo_exists():
            # don't count the next page button in index
            idx -= 1
            
        # Frame for each item
        item_frame = ctk.CTkFrame(self.results_frame)
        
        # Keep next page button at the bottom
        if self.next_page_btn is not None and self.next_page_btn.winfo_exists():
            self.next_page_btn.pack_forget()
            item_frame.pack(fill="x", padx=5, pady=5)
            self.next_page_btn.pack(pady=20)
        else:
            item_frame.pack(fill="x", padx=5, pady=5)"""
    content = content.replace(add_file_old, add_file_new)
    
    with open('main.py', 'w') as f:
        f.write(content)

patch_main()

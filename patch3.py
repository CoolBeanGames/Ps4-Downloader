import os

def patch_main():
    with open('main.py', 'r') as f:
        content = f.read()
        
    old_code = """    def perform_search(self, query, page=1):
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
                    
                    download_url = f"https://archive.org/download/{identifier}/{fname}"
                    
                    # Send to UI
                    self.after(0, self.add_file_to_ui, identifier, fname, fsize, upload_date, download_url)
        except Exception as e:
            print("Search error:", e)"""

    new_code = """    def perform_search(self, query, page=1):
        import requests
        import datetime
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ext_filter = self.ext_entry.get().strip().lower()
        if ext_filter and not ext_filter.startswith('.'):
            ext_filter = '.' + ext_filter
            
        def fetch_metadata(identifier):
            meta_url = f"https://archive.org/metadata/{identifier}"
            try:
                meta_resp = requests.get(meta_url, timeout=10)
                return identifier, meta_resp.json()
            except:
                return identifier, {}
                
        try:
            # 1. Search for items (Fetch up to 1000 items to get many more files)
            url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=1000&page={page}"
            response = requests.get(url, timeout=10)
            data = response.json()
            docs = data.get("response", {}).get("docs", [])
            
            with ThreadPoolExecutor(max_workers=20) as executor:
                futures = []
                for doc in docs:
                    identifier = doc.get("identifier")
                    if identifier:
                        futures.append(executor.submit(fetch_metadata, identifier))
                        
                for future in as_completed(futures):
                    identifier, meta_data = future.result()
                    files = meta_data.get("files", [])
                    
                    for f in files:
                        fname = f.get("name")
                        if not fname: continue
                        if ext_filter and not fname.lower().endswith(ext_filter):
                            continue
                        fsize = f.get("size", "0")
                        fmtime = f.get("mtime", "")
                        if fmtime:
                            try:
                                upload_date = datetime.datetime.fromtimestamp(int(fmtime)).strftime('%Y-%m-%d %H:%M')
                            except:
                                upload_date = "Unknown"
                        else:
                            upload_date = "Unknown"
                        
                        download_url = f"https://archive.org/download/{identifier}/{fname}"
                        
                        # Send to UI
                        self.after(0, self.add_file_to_ui, identifier, fname, fsize, upload_date, download_url)
        except Exception as e:
            print("Search error:", e)"""
            
    content = content.replace(old_code, new_code)
    with open('main.py', 'w') as f:
        f.write(content)
        
patch_main()

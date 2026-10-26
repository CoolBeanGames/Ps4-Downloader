import os

def patch_main():
    with open('main.py', 'r') as f:
        content = f.read()

    # 1. Add logging import and setup
    import_old = "import customtkinter as ctk"
    import_new = """import customtkinter as ctk
import logging

logging.basicConfig(
    filename='pkg_down.log',
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logging.info("Application started.")
"""
    content = content.replace(import_old, import_new)

    # 2. Add logging in on_search
    on_search_old = """            print(f"Searching for: {query}")"""
    on_search_new = """            logging.info(f"Searching for: {query}")"""
    content = content.replace(on_search_old, on_search_new)

    # 3. Add logging in fetch_metadata
    fetch_old = """            except Exception as e:
                print(f"Meta fetch error for {identifier}: {e}")"""
    fetch_new = """            except Exception as e:
                logging.error(f"Meta fetch error for {identifier}: {e}")"""
    content = content.replace(fetch_old, fetch_new)

    # 4. Add logging around advanced search API call
    adv_search_old = """        try:
            # 1. Search for items (Fetch up to 1000 items to get many more files)
            url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=1000&page={page}"
            response = requests.get(url, timeout=60)
            data = response.json()
            docs = data.get("response", {}).get("docs", [])
            
            with ThreadPoolExecutor(max_workers=5) as executor:"""
    adv_search_new = """        try:
            # 1. Search for items (Fetch up to 1000 items to get many more files)
            url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=1000&page={page}"
            logging.info(f"Fetching search results from: {url}")
            response = requests.get(url, timeout=60)
            data = response.json()
            docs = data.get("response", {}).get("docs", [])
            logging.info(f"Found {len(docs)} items for query '{query}'. Starting metadata fetch...")
            
            with ThreadPoolExecutor(max_workers=5) as executor:"""
    content = content.replace(adv_search_old, adv_search_new)
    
    # 5. Add logging to perform_search exception
    search_exc_old = """        except Exception as e:
            print("Search error:", e)"""
    search_exc_new = """        except Exception as e:
            logging.error(f"Search error: {e}")"""
    content = content.replace(search_exc_old, search_exc_new)

    # 6. Add logging to download functions
    start_dl_old = """    def start_download(self, identifier, fname, download_url, prg_bar, status_lbl, btn, dl_size_str):
        dest_dir = self.dest_entry.get().strip()"""
    start_dl_new = """    def start_download(self, identifier, fname, download_url, prg_bar, status_lbl, btn, dl_size_str):
        dest_dir = self.dest_entry.get().strip()
        logging.info(f"Queueing download: {fname}")"""
    content = content.replace(start_dl_old, start_dl_new)

    dl_worker_old = """                with open(out_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):"""
    dl_worker_new = """                logging.info(f"Starting to write {fname} to {out_path}")
                with open(out_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):"""
    content = content.replace(dl_worker_old, dl_worker_new)

    dl_exc_old = """            except Exception as e:
                print("Download error:", e)"""
    dl_exc_new = """            except Exception as e:
                logging.error(f"Download error for {fname}: {e}")"""
    content = content.replace(dl_exc_old, dl_exc_new)
    
    with open('main.py', 'w') as f:
        f.write(content)

patch_main()

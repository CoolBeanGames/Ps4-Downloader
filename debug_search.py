import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

def perform_search(query, page=1):
    ext_filter = ".pkg"
        
    def fetch_metadata(identifier):
        meta_url = f"https://archive.org/metadata/{identifier}"
        try:
            meta_resp = requests.get(meta_url, timeout=10)
            return identifier, meta_resp.json()
        except:
            return identifier, {}
            
    try:
        url = f"https://archive.org/advancedsearch.php?q={query}&output=json&rows=1000&page={page}"
        response = requests.get(url, timeout=10)
        data = response.json()
        docs = data.get("response", {}).get("docs", [])
        
        print(f"Found {len(docs)} docs")
        
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = []
            for doc in docs:
                identifier = doc.get("identifier")
                if identifier:
                    futures.append(executor.submit(fetch_metadata, identifier))
                    
            count = 0
            for future in as_completed(futures):
                identifier, meta_data = future.result()
                files = meta_data.get("files", [])
                
                for f in files:
                    fname = f.get("name")
                    if not fname: continue
                    if ext_filter and not fname.lower().endswith(ext_filter):
                        continue
                    count += 1
                    
        print(f"Found {count} files matching extension.")
    except Exception as e:
        print("Search error:", e)

perform_search("ps4 pkg")

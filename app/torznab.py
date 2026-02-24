from lxml import etree
import datetime
import logging

logger = logging.getLogger(__name__)

def build_caps() -> str:
    """Builds the capabilities XML response for Torznab client."""
    logger.debug("Building torznab capabilities XML")
    root = etree.Element("caps")
    
    server = etree.SubElement(root, "server", version="1.0", title="Audiobookbay Indexer", strapline="Audiobooks")
    
    searching = etree.SubElement(root, "searching")
    etree.SubElement(searching, "search", available="yes", supportedParams="q")
    etree.SubElement(searching, "book-search", available="yes", supportedParams="q,author,title")
    
    categories = etree.SubElement(root, "categories")
    cat = etree.SubElement(categories, "category", id="3000", name="Audio")
    etree.SubElement(cat, "subcat", id="3030", name="Audio/Audiobook")
    
    return etree.tostring(root, xml_declaration=True, encoding="utf-8").decode()

def build_rss(results: list, host_url: str, offset: int = 0) -> str:
    """Builds the RSS feed containing the search results."""
    logger.debug(f"Building RSS feed for {len(results)} results with host_url {host_url}, offset {offset}")
    TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
    ATOM_NS = "http://www.w3.org/2005/Atom"
    
    rss = etree.Element("rss", version="1.0", nsmap={"atom": ATOM_NS, "torznab": TORZNAB_NS})
    channel = etree.SubElement(rss, "channel")
    
    etree.SubElement(channel, f"{{{ATOM_NS}}}link", rel="self", type="application/rss+xml")
    etree.SubElement(channel, "title").text = "Audiobookbay Indexer"
    
    total = offset + len(results)
    if len(results) > 0:
        total += 1000
    etree.SubElement(channel, f"{{{TORZNAB_NS}}}response", offset=str(offset), total=str(total))

    for res in results:
        item = etree.SubElement(channel, "item")
        
        # Determine Title
        title = res.get("title", "Unknown Title")
        etree.SubElement(item, "title").text = title
        
        # Enclose Description (Empty if none provided)
        description = res.get("description", "")
        etree.SubElement(item, "description").text = description
        
        dl_url = f"{host_url}/api/download?url={res.get('link', '')}"
        
        # GUID should typically be the unique tracker link, or download link as fallback
        guid_val = res.get("link", dl_url)
        etree.SubElement(item, "guid").text = guid_val
        
        # Prowlarr Indexer node is sometimes expected in Torznab proxies, though usually injected by the proxy itself
        # etree.SubElement(item, "prowlarrindexer", id="5", type="public").text = "Audiobookbay"
        
        comments = res.get("link", "")
        etree.SubElement(item, "comments").text = comments
        
        pub_date = datetime.datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        etree.SubElement(item, "pubDate").text = pub_date
        
        size = str(res.get("size_bytes", 0))
        etree.SubElement(item, "size").text = size
        
        etree.SubElement(item, "link").text = dl_url
        
        # Torznab Standard Categories for Audio/Audiobooks
        etree.SubElement(item, "category").text = "3000"
        etree.SubElement(item, "category").text = "3030"
        
        # Enclosure containing output structure.
        # Since we use magnet redirects, `application/x-bittorrent` is the standard acceptable type for torznab
        etree.SubElement(item, "enclosure", url=dl_url, length=size, type="application/x-bittorrent")
        
        # Torznab Attributes
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="category", value="3000")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="category", value="3030")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="genre", value="")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="seeders", value=str(res.get("seeders", "0")))
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="files", value=str(res.get("files", "1")))
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="grabs", value=str(res.get("grabs", "0")))
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="peers", value=str(res.get("peers", "0")))
        
        author = res.get("author", "")
        if author:
            etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="author", value=author)
            
        booktitle = res.get("title", "Unknown Title")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="booktitle", value=booktitle)
        
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="minimumratio", value="1")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="minimumseedtime", value="259200")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="downloadvolumefactor", value="1")
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="uploadvolumefactor", value="1")
            
    return etree.tostring(rss, xml_declaration=True, encoding="utf-8").decode()

from lxml import etree
import datetime

def build_caps() -> str:
    """Builds the capabilities XML response for Torznab client."""
    root = etree.Element("torznab")
    
    server = etree.SubElement(root, "server", version="1.0", title="Audiobookbay Indexer", strapline="Audiobooks")
    
    searching = etree.SubElement(root, "searching")
    etree.SubElement(searching, "search", available="yes", supportedParams="q")
    etree.SubElement(searching, "book-search", available="yes", supportedParams="q,author,title")
    
    categories = etree.SubElement(root, "categories")
    cat = etree.SubElement(categories, "category", id="3000", name="Audio")
    etree.SubElement(cat, "subcat", id="3030", name="Audio/Audiobook")
    
    return etree.tostring(root, xml_declaration=True, encoding="utf-8").decode()

def build_rss(results: list, host_url: str) -> str:
    """Builds the RSS feed containing the search results."""
    TORZNAB_NS = "http://torznab.com/schemas/2015/feed"
    rss = etree.Element("rss", version="2.0", nsmap={"torznab": TORZNAB_NS})
    channel = etree.SubElement(rss, "channel")
    
    etree.SubElement(channel, "title").text = "Audiobookbay Indexer"
    etree.SubElement(channel, "description").text = "Search results from Audiobookbay"
    etree.SubElement(channel, "link").text = host_url
    
    for res in results:
        item = etree.SubElement(channel, "item")
        etree.SubElement(item, "title").text = res.get("title", "Unknown Title")
        
        # Link to the simulated download endpoint
        dl_url = f"{host_url}/api/download?url={res['link']}"
        etree.SubElement(item, "link").text = dl_url
        etree.SubElement(item, "guid").text = dl_url
        etree.SubElement(item, "comments").text = res.get("link", "")
        etree.SubElement(item, "pubDate").text = datetime.datetime.now().strftime("%a, %d %b %Y %H:%M:%S +0000")
        etree.SubElement(item, "size").text = str(res.get("size_bytes", 0))
        etree.SubElement(item, "category").text = "3030"
        
        # Torznab Attributes
        etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="category", value="3030")
        if res.get("author"):
            etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="author", value=res.get("author"))
        if res.get("title"):
            etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="title", value=res.get("title"))
        if res.get("size_str"):
            etree.SubElement(item, f"{{{TORZNAB_NS}}}attr", name="size", value=res.get("size_str"))
            
    return etree.tostring(rss, xml_declaration=True, encoding="utf-8").decode()

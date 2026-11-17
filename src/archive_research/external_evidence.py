"""Read explicit retained web references; no crawler, network fetch, or HTML execution."""
from html.parser import HTMLParser
import json
from urllib.parse import urlsplit

from .contracts import Source, require


def read_reference(roots,binding,image_source):
    record_source=Source(**binding)
    record=json.loads(roots.source(record_source,verify_hash=True).read_text())
    fields={'format','url','image_url','accessed_at','attribution','reuse_terms','quote',
            'summary','comparison','lineage_basis','image_source','page_source'}
    require(set(record)==fields and record['format']=='archive-web-reference-1','Invalid web reference')
    for name in ('url','image_url'):
        url=urlsplit(record[name]);require(url.scheme=='https' and url.hostname and not url.username,
                                          'Public HTTPS reference required')
    for name in fields-{'format','image_source','page_source'}:
        require(isinstance(record[name],str) and 0<len(record[name])<=5000,'Invalid web reference text')
    require(Source(**record['image_source'])==image_source,'Web image belongs to another source')
    roots.source(image_source,verify_hash=True)
    page=roots.source(Source(**record['page_source']),verify_hash=True)
    require(page.stat().st_size<=5_000_000,'Web snapshot too large')
    class Text(HTMLParser):
        def __init__(self):super().__init__();self.parts=[]
        def handle_data(self,data):self.parts.append(data)
    parser=Text();parser.feed(page.read_text())
    require(' '.join(record['quote'].split()) in ' '.join(' '.join(parser.parts).split()),
            'Web quotation absent from retained page')
    return {k:v for k,v in record.items() if k not in ('image_source','page_source')}

import hashlib
import json
from dataclasses import asdict,replace
import unittest
import test_research_evidence as fixtures
from archive_research.external_evidence import read_reference
from archive_research.evidence import build_packet
from research_routes import research_profiles


class ExternalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.EvidenceTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.image=self.fixture.page.source
        self.record={'format':'archive-web-reference-1','url':'https://example.org/page','image_url':'https://example.org/image.png',
                     'accessed_at':'2026-11-17','attribution':'Invented fixture','reuse_terms':'Synthetic only',
                     'quote':'Invented caption','summary':'Synthetic source','comparison':'No real identity',
                     'lineage_basis':'Invented independent reference','image_source':asdict(self.image),
                     'page_source':asdict(self.file('page.html',b'<h1>Invented <b>caption</b></h1>'))}

    def file(self,name,data):
        p=self.fixture.input/name;p.write_bytes(data);s=p.stat()
        return replace(self.image,path=name,size_bytes=s.st_size,mtime_ns=s.st_mtime_ns,
                       sha256=hashlib.sha256(data).hexdigest())

    def binding(self):return asdict(self.file('reference.json',json.dumps(self.record).encode()))

    def test_bound_snapshot_and_literal_quote(self):
        # Give the existing synthetic image a content fingerprint, as real references require.
        self.image=replace(self.image,sha256=hashlib.sha256((self.fixture.input/self.image.path).read_bytes()).hexdigest())
        self.record['image_source']=asdict(self.image)
        value=read_reference(self.fixture.roots,self.binding(),self.image)
        self.assertEqual(value['quote'],'Invented caption');self.assertNotIn('page_source',value)

    def test_wrong_image_and_unsupported_quote_refused(self):
        with self.assertRaises(ValueError):read_reference(self.fixture.roots,self.binding(),replace(self.image,object_id='other'))
        self.image=replace(self.image,sha256=hashlib.sha256((self.fixture.input/self.image.path).read_bytes()).hexdigest())
        self.record['image_source']=asdict(self.image);self.record['quote']='invented missing quotation'
        with self.assertRaisesRegex(ValueError,'quotation absent'):read_reference(self.fixture.roots,self.binding(),self.image)

    def test_changed_snapshot_is_not_silently_accepted(self):
        self.image=replace(self.image,sha256=hashlib.sha256((self.fixture.input/self.image.path).read_bytes()).hexdigest())
        self.record['image_source']=asdict(self.image);binding=self.binding()
        (self.fixture.input/'page.html').write_text('changed')
        with self.assertRaises(ValueError):read_reference(self.fixture.roots,binding,self.image)


if __name__=='__main__':unittest.main()

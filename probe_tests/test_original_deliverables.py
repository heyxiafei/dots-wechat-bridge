"""Actual byte/crypto path with synthetic files and an in-process fake client.

No HTTP, owner account or phone is used. MP4 verifies container/protocol only.
"""
import hashlib
import io
import unittest
import zipfile
from PIL import Image
import test_outbound as fixture


def pdf_bytes():
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',
             b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
             b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>']
    out=b'%PDF-1.4\n';offsets=[]
    for n,obj in enumerate(objects,1):
        offsets.append(len(out));out+=str(n).encode()+b' 0 obj\n'+obj+b'\nendobj\n'
    start=len(out);out+=b'xref\n0 4\n0000000000 65535 f \n'
    out+=b''.join(('%010d 00000 n \n'%x).encode() for x in offsets)
    return out+b'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n'+str(start).encode()+b'\n%%EOF\n'


def docx_bytes():
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic original document</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def mp4_structure():
    def box(kind,data):return (len(data)+8).to_bytes(4,'big')+kind+data
    h=box(b'hdlr',b'\0'*8+b'vide'+b'\0'*12)
    return box(b'ftyp',b'mp42'+b'\0'*4+b'mp42isom')+box(b'moov',box(b'trak',box(b'mdia',h)))+box(b'mdat',b'\0'*16)


class OriginalDeliverableTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.OutboundTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_four_attachments_keep_original_bytes_independent_ids_after_text_final(self):
        f=self.f
        f.probe.weixin.queue_reply(f.mid,'completed text remains immutable')
        with f.probe.db:f.probe.db.execute("UPDATE weixin_outbox SET status='accepted' WHERE message_id=?",(f.mid,))
        png=io.BytesIO();Image.new('RGB',(8,8),(10,20,30)).save(png,'PNG')
        cases=[('image','preview.png',png.getvalue(),2),('file','requested.pdf',pdf_bytes(),4),
               ('file','requested.docx',docx_bytes(),4),('video','requested.mp4',mp4_structure(),5)]
        ids=[]
        for i,(kind,name,raw,protocol) in enumerate(cases):
            uid=f.ready(raw,identity='upload-'+str(i),kind=kind,filename=name)
            sid=f.queue(uid,'send-'+str(i));ids.append(sid)
            self.assertTrue(f.probe.weixin.send_once())
            status=f.probe.weixin.send_status(sid)
            self.assertEqual(status['status'],'accepted');self.assertIsNone(status['delivered'])
            self.assertEqual(f.client.sent[-1][1][0]['type'],protocol)
            self.assertEqual(hashlib.sha256(f.uploads[-1][0]).digest(),hashlib.sha256(raw).digest())
            self.assertEqual(f.queue(uid,'send-'+str(i)),sid)
            self.assertFalse(f.probe.weixin.send_once())
            if kind=='file':
                item=f.client.sent[-1][1][0]['file_item']
                self.assertEqual((item['file_name'],item['len']),(name,str(len(raw))))
        self.assertEqual(len(set(ids)),4)
        self.assertEqual(len(f.client.sent),4)
        self.assertEqual(f.probe.weixin.send_status(f.mid)['outgoing_text'],'completed text remains immutable')

    def test_pdf_and_word_have_distinct_keys_even_for_one_owner_request(self):
        from probe.deliverable_contract import preview_commitment
        pdf=preview_commitment('owner','request','result','pdf',hashlib.sha256(pdf_bytes()).hexdigest())
        doc=preview_commitment('owner','request','result','docx',hashlib.sha256(docx_bytes()).hexdigest())
        self.assertNotEqual(pdf['key'],doc['key'])

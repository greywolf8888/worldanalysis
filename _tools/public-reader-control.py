"""One bounded, credential-free real public read; never a course-policy verifier."""
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
from worldanalysis.store import Store
from worldanalysis.analysis import Analysis

URL = 'https://example.com/'
HOST = 'example.com'


def reader(url, *, max_bytes):
    if url != URL or max_bytes != 262144:
        raise ValueError('control source scope mismatch')
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM)})
    if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
        raise ValueError('public IP required')
    class PinnedHTTPS(http.client.HTTPSConnection):
        def connect(self):
            raw = socket.create_connection((addresses[0],443),timeout=12)
            try:
                self.sock = self._context.wrap_socket(raw,server_hostname=HOST)
            except BaseException:
                raw.close()
                raise
    connection = PinnedHTTPS(HOST,timeout=12,context=ssl.create_default_context())
    try:
        connection.request('GET','/',headers={'Host':HOST,'Accept-Encoding':'identity','User-Agent':'Worldanalysis-Public-Read/1'})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('public source refused; no redirect or retry')
        if response.getheader('Content-Encoding') not in (None,'identity'):
            raise ValueError('encoded response unsupported')
        chunks,total = [],0
        while True:
            chunk = response.read(min(16384,max_bytes-total+1))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError('public read byte limit')
            chunks.append(chunk)
        body = b''.join(chunks)
        text = body.decode('utf-8','strict')
        title = re.search(r'<title>([^<]+)</title>',text,re.I)
        if not title:
            raise ValueError('public title missing')
        return {'final_url':url,'status':200,'text':text,'bytes_received':total,'read_complete':True,'reader_id':'pinned-public-get-control-v1','sha256':hashlib.sha256(body).hexdigest(),'title':title.group(1)}
    finally:
        connection.close()


if __name__ == '__main__':
    store = Store('G:/Worldanalysis')
    analysis = Analysis(store)
    try:
        result = analysis.read_external('analysis-task-9ea1b8827fc54a56a25bf1296732e86a','public-control-example-domain',URL,reader=reader,allowed_hosts=(HOST,))
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        store.close()

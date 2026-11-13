import io
import threading
import unittest
from worldanalysis.reader import probe_range, BoundedReader, ByteBoundedQueue
from worldanalysis.policy import ByteBudget, GateError


class Response:
    def __init__(self,status=206,body=b'abcd',headers=None):
        self.status=status; self.headers={'Content-Range':'bytes 0-3/20','Content-Length':'4'} if headers is None else headers
        self.stream=io.BytesIO(body);self.reads=[];self.closed=False
    def read(self,n): self.reads.append(n);return self.stream.read(n)
    def close(self): self.closed=True


class ReaderTests(unittest.TestCase):
    def test_ignored_200_never_reads_body(self):
        response=Response(status=200)
        result=probe_range(lambda *_:response,start=0,end=3,budget=ByteBudget(4))
        self.assertEqual(result['state'],'unsupported');self.assertEqual(response.reads,[]);self.assertTrue(response.closed)

    def test_real_byte_accounting_and_exact_206(self):
        response=Response();budget=ByteBudget(6)
        result=probe_range(lambda *_:response,start=0,end=3,budget=budget)
        self.assertEqual(result['received_bytes'],4);self.assertEqual(budget.received,4)
        with self.assertRaises(GateError): probe_range(lambda *_:self.fail('must not open'),start=0,end=3,budget=budget)

    def test_invalid_headers_short_body_and_permission_fail_closed(self):
        for response in [Response(headers={'Content-Range':'bytes 0-4/20'}),Response(headers={'Content-Range':'bytes 0-3/3'}),Response(headers={'Content-Range':'not valid'}),Response(body=b'a'),Response(status=403)]:
            with self.subTest(response=response),self.assertRaises(GateError):
                probe_range(lambda *_:response,start=0,end=3,budget=ByteBudget(10))
            self.assertTrue(response.closed)

    def test_bounded_reads_revalidate_every_time(self):
        calls=[];reader=BoundedReader(io.BytesIO(b'abcdef'),ByteBudget(4),revalidate=lambda:calls.append(True))
        with self.assertRaises(GateError): reader.read(-1)
        self.assertEqual(reader.read(2),b'ab');self.assertEqual(reader.read(8),b'cd')
        with self.assertRaises(GateError): reader.read(1)
        self.assertEqual(len(calls),3)

    def test_queue_rejects_oversize_without_evicting_evidence(self):
        queue=ByteBoundedQueue(1,4)
        with self.assertRaises(GateError):queue.put(b'12345')
        queue.put(b'abcd')
        with self.assertRaises(GateError):queue.put(b'x',timeout=.001)
        self.assertEqual(queue.get(),b'abcd');self.assertEqual(queue.resident_bytes,0)

    def test_consumer_releases_backpressure(self):
        queue=ByteBoundedQueue(1,4);queue.put(b'abcd');finished=threading.Event()
        def producer():queue.put(b'xy',timeout=2);finished.set()
        thread=threading.Thread(target=producer);thread.start()
        self.assertFalse(finished.is_set());self.assertEqual(queue.get(),b'abcd')
        thread.join(2);self.assertTrue(finished.is_set());self.assertEqual(queue.get(),b'xy')


if __name__=='__main__':unittest.main()

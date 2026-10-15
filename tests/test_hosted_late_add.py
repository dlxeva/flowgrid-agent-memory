"""Synthetic late Add completion after HTTP timeout, never real user data."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from aml_retriever.aml_hosted import HostedAMLServer, HostedConfig


class HostedLateAddTests(unittest.TestCase):
    def test_504_keeps_slot_then_late_add_and_same_id_retry_are_idempotent(self):
        with tempfile.TemporaryDirectory(prefix='fg-late-add-') as temporary:
            server=HostedAMLServer(HostedConfig(credential='synthetic-late-add-key',
                db_path=str(Path(temporary)/'synthetic.db'),port=0,max_inflight=1,
                operation_timeout_seconds=0.05,request_timeout_seconds=1),quiet=True)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            release=threading.Event();started=threading.Event();finished=threading.Event()
            original=server.service.official_add
            payload={'request_id':'synthetic-late-id','user_id':'synthetic-late-user',
                'session_id':'synthetic-late-session','messages':[{'role':'user','content':'synthetic late marker'}]}

            def slow_add(value):
                started.set()
                if not release.wait(2):raise RuntimeError('synthetic fixture wait expired')
                try:return original(value)
                finally:finished.set()

            def call():
                request=urllib.request.Request(f'http://127.0.0.1:{server.server_address[1]}/add',
                    data=json.dumps(payload).encode(),method='POST',
                    headers={'Content-Type':'application/json','Authorization':'Bearer synthetic-late-add-key'})
                try:response=urllib.request.urlopen(request,timeout=3)
                except urllib.error.HTTPError as error:response=error
                with response:return response.status,json.loads(response.read())

            try:
                with mock.patch.object(server.service,'official_add',side_effect=slow_add):
                    status,_=call()
                    self.assertTrue(started.is_set());self.assertEqual(status,504)
                    self.assertEqual(server.service.db.count('synthetic-late-user'),0)
                    self.assertEqual(call()[0],429)
                    release.set();self.assertTrue(finished.wait(2))
                    deadline=time.monotonic()+2
                    while not server.operations.acquire(blocking=False):
                        if time.monotonic()>=deadline:self.fail('synthetic slot did not recover')
                        time.sleep(0.005)
                    server.operations.release()
                self.assertEqual(server.service.db.count('synthetic-late-user'),1)
                self.assertEqual(call()[0],200)
                self.assertEqual(server.service.db.count('synthetic-late-user'),1)
                self.assertEqual(server.service.search_governed(user_id='synthetic-late-user').records,[])
            finally:
                release.set();server.shutdown();server.server_close();server.service.close();thread.join(2)
                self.assertFalse(thread.is_alive())


if __name__=='__main__':unittest.main()

"""All provider I/O mocked; diagnostic failures must never release a hold."""
import json
import io
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import urllib.error

from aml_retriever import aml_semantic_cues as cues


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.events=[];self.financial=[]
        self.wire=cues.request('search',{'query':'pens','options':None},provider='deepseek')
        self.transport=cues.DeepSeekCueTransport('synthetic-secret',enabled=True,
            reserve=lambda *_:self.financial.append('reserve') or 'slot',
            settle=lambda *_:self.financial.append('settle'),
            unknown=lambda *_:self.financial.append('unknown'),
            observe=lambda event,raw:self.events.append((event,raw)))

    def failed(self,process):
        with patch.object(cues.subprocess,'run',return_value=process) as worker:
            with self.assertRaises(cues.ApiError) as error:self.transport(self.wire)
            self.assertEqual(worker.call_count,1)
        self.assertNotIn('synthetic-secret',str(error.exception))
        self.assertEqual(self.financial,['reserve','unknown'])

    def test_http_status_and_bounded_raw_body_without_error_text_in_metadata(self):
        body=b'{"error":"untrusted provider text"}'
        self.failed(SimpleNamespace(returncode=2,stdout=body,stderr=b'{"kind":"http_error","http_status":403,"message":"synthetic-secret"}'))
        self.assertEqual(self.events,[({'kind':'http_error','http_status':403},body)])

    def test_unknown_worker_stderr_is_not_copied_into_diagnostics(self):
        self.failed(SimpleNamespace(returncode=2,stdout=b'',stderr=b'synthetic-secret'))
        self.assertEqual(self.events,[({'kind':'worker_error'},None)])

    def test_invalid_json_archived_before_completion_error(self):
        self.failed(SimpleNamespace(returncode=0,stdout=b'invalid-json'))
        self.assertEqual(self.events[0],({'kind':'provider_response'},b'invalid-json'))
        self.assertEqual(self.events[1],({'kind':'completion_validation_error'},None))

    def test_finish_length_archived_and_classified(self):
        body=json.dumps({'model':cues.DEEPSEEK_MODEL,'choices':[{'finish_reason':'length','message':{'role':'assistant','content':'{}'}}]}).encode()
        self.failed(SimpleNamespace(returncode=0,stdout=body))
        self.assertEqual(self.events[0][1],body)
        self.assertEqual(self.events[1][0],{'kind':'completion_validation_error','code':'cue_incomplete'})

    def test_process_timeout_preserves_partial_output_without_exception_details(self):
        with patch.object(cues.subprocess,'run',side_effect=subprocess.TimeoutExpired('synthetic-secret',25,output=b'partial')) as worker:
            with self.assertRaises(cues.ApiError):self.transport(self.wire)
            self.assertEqual(worker.call_count,1)
        self.assertEqual(self.events,[({'kind':'process_timeout'},b'partial')])
        self.assertEqual(self.financial,['reserve','unknown'])

    def test_sink_failure_fails_closed_before_settlement(self):
        def broken(*_):raise OSError('synthetic-secret')
        self.transport.observe=broken
        self.failed(SimpleNamespace(returncode=0,stdout=b'{}'))
        self.assertEqual(self.events,[])

    def test_network_timeout_is_distinct_from_network_error(self):
        for kind in ['network_timeout','network_error','response_size_limit']:
            with self.subTest(kind=kind):
                self.setUp()
                self.failed(SimpleNamespace(returncode=2,stdout=b'',stderr=json.dumps({'kind':kind}).encode()))
                self.assertEqual(self.events,[({'kind':kind},None)])

    def worker(self,*,body=b'',error=None):
        # Execute the real isolated worker program with its network opener
        # replaced. No credentials or external requests are ever used.
        packet=cues.encode({'credential':'synthetic-secret','provider':'deepseek','request':self.wire})
        stdin=SimpleNamespace(buffer=io.BytesIO(packet));out=io.BytesIO();err=io.StringIO()
        response=SimpleNamespace(read=lambda _:body)
        class Context:
            def __enter__(self):return response
            def __exit__(self,*_):pass
        opener=SimpleNamespace(open=lambda *_a,**_k:Context())
        if error is not None:
            def failed(*_a,**_k):raise error
            opener.open=failed
        with patch.object(cues.sys,'stdin',stdin),patch.object(cues.sys,'stdout',SimpleNamespace(buffer=out)),patch.object(cues.sys,'stderr',err),patch('urllib.request.build_opener',return_value=opener):
            try:exec(compile(cues._WORKER,'<synthetic-cue-worker>','exec'),{})
            except SystemExit as exit:self.assertEqual(exit.code,2)
        return out.getvalue(),json.loads(err.getvalue()) if err.getvalue() else None

    def test_actual_worker_http_error_preserves_body_and_status_only(self):
        error=urllib.error.HTTPError('https://example.invalid/synthetic-secret',403,'synthetic-secret',{},io.BytesIO(b'denied-body'))
        raw,event=self.worker(error=error)
        self.assertEqual(raw,b'denied-body');self.assertEqual(event,{'kind':'http_error','http_status':403})

    def test_actual_worker_classifies_network_errors(self):
        for error,kind in [(TimeoutError('synthetic-secret'),'network_timeout'),(urllib.error.URLError(TimeoutError()),'network_timeout'),(urllib.error.URLError('synthetic-secret'),'network_error')]:
            with self.subTest(kind=kind):
                raw,event=self.worker(error=error)
                self.assertEqual(raw,b'');self.assertEqual(event,{'kind':kind})

    def test_actual_worker_keeps_bounded_oversize_prefix(self):
        raw,event=self.worker(body=b'x'*16385)
        self.assertEqual(len(raw),16385);self.assertEqual(event,{'kind':'response_size_limit'})

    def test_actual_worker_success_leaves_raw_response_untouched(self):
        raw,event=self.worker(body=b'{"synthetic":"body"}')
        self.assertEqual(raw,b'{"synthetic":"body"}');self.assertIsNone(event)


if __name__=='__main__':unittest.main()

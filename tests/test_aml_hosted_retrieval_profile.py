"""S0 real loopback parity with temporary synthetic state; no paid calls."""
import json
import os
import threading
import unittest
import urllib.request
from unittest.mock import patch
from aml_retriever.aml_hosted import (HostedConfig, HostedAMLServer,
    HostedConfigurationError, hosted_retrieval_config, retrieval_config_digest)
from aml_retriever.api import MemoryService

KEY = 'synthetic-retrieval-test-key'


class HostedRetrievalProfileTests(unittest.TestCase):
    def config(self, profile='{}'):
        return HostedConfig(credential=KEY, db_path=':memory:', port=0, retrieval_json=profile)

    def test_invalid_profiles_fail_closed(self):
        for raw in ('[]', '{"auth_mode":"none"}', '{"db_path":"/secret"}',
                    '{"host":"0.0.0.0"}', '{"top_k_max":999}',
                    '{"flags":{"views":"false"}}', '{"flags":{"unknown":true}}',
                    '{"flags":{"vector":true}}', '{"max_candidates":0}',
                    '{"max_candidates":true}', '{"rrf_weight_feature":NaN}',
                    '{"rrf_k":1,"rrf_k":2}', '{"window_overlap":3}',
                    '{"rrf_weight_feature":1e999}', ' ' * 8193):
            with self.subTest(raw=raw), self.assertRaisesRegex(HostedConfigurationError,
                    '^AML hosted configuration rejected$'):
                self.config(raw)

    def test_environment_profile_does_not_read_legacy(self):
        with patch.dict(os.environ, {'AML_HOSTED_KEY': KEY, 'AML_HOSTED_DB_PATH': ':memory:',
                'AML_HOSTED_RETRIEVAL_JSON': '{"flags":{"views":false}}',
                'AML_CONFIG': '/nonexistent-secret', 'AML_AUTH_MODE': 'none',
                'AML_FLAG_VIEWS': '1'}, clear=True):
            c = HostedConfig.from_env()
            self.assertFalse(hosted_retrieval_config(c).flags['views'])
            self.assertEqual(c.auth_mode, 'bearer')

    def test_digest_is_semantic_and_excludes_secrets(self):
        a = hosted_retrieval_config(self.config('{"rrf_k":61,"flags":{"views":false}}'))
        b = hosted_retrieval_config(self.config('{ "flags": {"views": false}, "rrf_k": 61 }'))
        self.assertEqual(retrieval_config_digest(a), retrieval_config_digest(b))
        self.assertNotEqual(retrieval_config_digest(a), retrieval_config_digest(hosted_retrieval_config(self.config())))
        self.assertNotIn(KEY, repr(self.config()))

    def test_custom_service_and_profile_cannot_disagree(self):
        with MemoryService() as service:
            with self.assertRaises(HostedConfigurationError):
                HostedAMLServer(self.config('{"rrf_k":61}'), service=service)

    def test_real_http_profile_and_direct_search_match(self):
        all_ids = []
        for profile in ('{}', '{"flags":{"views":false,"rrf":false,"rerank":false,"dedup":false}}'):
            with self.subTest(profile=profile):
                cfg = self.config(profile)
                server = HostedAMLServer(cfg, quiet=True)
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                base = 'http://127.0.0.1:' + str(server.server_port)
                def http(route, body=None, key=KEY):
                    request = urllib.request.Request(base + route,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.load(response)
                try:
                    p = {'request_id':'synthetic-request','user_id':'synthetic-user',
                        'session_id':'synthetic-session','messages':[
                        {'role':'user','content':'I like comet tea.'},
                        {'role':'assistant','content':'Comet tea is recorded.'},
                        {'role':'user','content':'Comet tea preference remains.'}]}
                    self.assertTrue(http('/add', p)['success'])
                    self.assertTrue(http('/add', p)['success'])
                    q = {'user_id':p['user_id'],'query':'comet tea','top_k':100}
                    result = http('/search', q)
                    self.assertEqual(result, server.service.official_search(q))
                    expected = hosted_retrieval_config(cfg)
                    self.assertEqual(server.service.config.flags, expected.flags)
                    self.assertEqual(http('/health')['retrieval_config_digest_sha256'], retrieval_config_digest(expected))
                    self.assertLessEqual(len(result['data']), 100)
                    self.assertEqual(http('/search', {**q, 'user_id':'other-user'}), {'data':[]})
                    all_ids.append([r['id'] for r in result['data']])
                finally:
                    server.shutdown()
                    server.server_close()
                    server.service.close()
                    worker.join(timeout=5)
                    self.assertFalse(worker.is_alive())
        self.assertNotEqual(all_ids[0], all_ids[1], 'profile must change real response, not just metadata')

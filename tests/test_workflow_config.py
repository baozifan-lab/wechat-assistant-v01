import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from zhishi.models.workflow import WorkflowModel
from zhishi.models.workflow_config import WorkflowConfig, _dpapi, normalize_base


@unittest.skipUnless(os.name == 'nt', 'Windows user encryption')
class WorkflowConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'data'/'workflow-model.json'
        self.model = WorkflowModel(self.path)
        self.data = {'base_url':'https://example.test/v1','model':'qwen3.8-max','api_key':'dummy-private-key'}

    def tearDown(self):
        self.temp.cleanup()

    def test_dpapi_round_trip_and_no_plaintext_key_on_disk_or_in_settings(self):
        ciphertext = _dpapi(b'dummy-private-key')
        self.assertNotIn(b'dummy-private-key', ciphertext)
        self.assertEqual(_dpapi(ciphertext, decrypt=True), b'dummy-private-key')
        settings = self.model.configure(self.data)
        self.assertNotIn('dummy-private-key', self.path.read_text('utf-8'))
        self.assertNotIn('dummy-private-key', json.dumps(settings))
        self.assertTrue(settings['ready'])
        self.assertTrue(settings['has_key'])
        restarted = WorkflowModel(self.path)
        self.assertEqual(restarted.config.read()['api_key'], 'dummy-private-key')
        self.assertEqual(restarted.settings()['model'], 'qwen3.8-max')

    def test_blank_key_kept_for_same_endpoint_but_not_transferred_to_new_endpoint(self):
        self.model.configure(self.data)
        self.model.configure({**self.data, 'api_key':''})
        self.assertEqual(self.model.config.read()['api_key'], 'dummy-private-key')
        settings = self.model.configure({**self.data,'base_url':'https://other.example/v1','api_key':''})
        self.assertFalse(settings['has_key'])
        self.assertFalse(settings['ready'])

    def test_clear_key_preserves_endpoint_and_blocks_calls(self):
        self.model.configure(self.data)
        settings = self.model.configure({**self.data,'api_key':'','clear_key':True})
        self.assertEqual(settings['base_url'], self.data['base_url'])
        self.assertFalse(settings['has_key'])
        with patch.object(WorkflowModel, '_open') as call:
            result = self.model.test_connection()
        self.assertFalse(result['ok'])
        call.assert_not_called()

    def test_invalid_endpoint_or_key_does_not_overwrite_saved_configuration(self):
        self.model.configure(self.data)
        for updates in [{'base_url':'https://user:key@example.test/v1'},
                        {'base_url':'https://example.test/v1?key=secret'},
                        {'api_key':'secret\r\ninjection'}, {'model':'3。8max'}, {'json_mode':'yes'}]:
            with self.assertRaises(ValueError):
                self.model.configure({**self.data, **updates})
            self.assertEqual(self.model.config.read()['api_key'], 'dummy-private-key')

    def test_saved_model_uses_key_without_requiring_environment_alias_edit(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, limit):
                return json.dumps({'choices':[{'message':{'content':json.dumps({'action':'accept','confidence':.99,'reason':'明确同意'})}}]}).encode()
        self.model.configure({**self.data,'disable_thinking':True,'json_mode':True})
        with patch.dict(os.environ, {'MODEL_ALIASES':'reminder-fast,planner-strong'}):
            with patch.object(WorkflowModel, '_open', return_value=Response()) as call:
                result = self.model.test_connection()
        request = call.call_args.args[0]
        self.assertEqual(request.get_header('Authorization'), 'Bearer dummy-private-key')
        payload = json.loads(request.data)
        self.assertEqual(payload['model'], 'qwen3.8-max')
        self.assertFalse(payload['enable_thinking'])
        self.assertEqual(payload['response_format'], {'type':'json_object'})
        self.assertTrue(result['ok'])
        self.assertTrue(self.model.settings()['test']['ok'])
        self.model.configure({**self.data, 'model':'a-different-model'})
        self.assertIsNone(self.model.settings()['test'])

    def test_provider_error_body_and_key_are_not_returned_to_browser(self):
        self.model.configure(self.data)
        error = urllib.error.HTTPError('https://example.test/v1/chat/completions', 401, 'Unauthorized', {}, io.BytesIO(b'dummy-private-key private-diagnostic'))
        with patch.object(WorkflowModel, '_open', side_effect=error):
            result = self.model.test_connection()
        self.assertFalse(result['ok'])
        self.assertIn('API Key', result['detail'])
        self.assertNotIn('dummy-private-key', json.dumps(result))
        self.assertNotIn('private-diagnostic', json.dumps(result))

    def test_connection_test_does_not_touch_task_database(self):
        self.model.configure(self.data)
        with patch.object(WorkflowModel, '_open', side_effect=urllib.error.URLError('offline')):
            self.assertFalse(self.model.test_connection()['ok'])
        self.assertEqual(list(self.path.parent.glob('*.sqlite3')), [])


class EndpointTests(unittest.TestCase):
    def test_normalize_base_or_full_completion_url(self):
        self.assertEqual(normalize_base(' https://example.test/v1/chat/completions/ '), 'https://example.test/v1')
        self.assertEqual(normalize_base('https://example.test/'), 'https://example.test/v1')
        self.assertEqual(normalize_base('https://example.test/api/openai/v1'), 'https://example.test/api/openai/v1')

    def test_redirect_does_not_forward_bearer_key(self):
        from zhishi.models.workflow import NoRedirect
        handler = NoRedirect()
        request = __import__('urllib.request', fromlist=['Request']).Request('https://example.test/v1', headers={'Authorization':'Bearer dummy'})
        self.assertIsNone(handler.redirect_request(request, None, 302, 'Found', {}, 'https://other.example/v1'))


class ResponseDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ,{'WORKFLOW_MODEL_BASE_URL':'https://example.test/v1',
                                      'WORKFLOW_MODEL':'planner-strong','MODEL_ALIASES':'planner-strong'},clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def interpret_body(self, body):
        with patch.object(WorkflowModel,'_open',return_value=io.BytesIO(body)):
            return WorkflowModel().interpret({'role':'负责人'},'同意')

    def test_timeout_is_distinct_from_invalid_json_and_uses_longer_deadline(self):
        with patch.object(WorkflowModel,'_open',side_effect=TimeoutError()):
            with self.assertRaisesRegex(ValueError,'超过 90 秒'):
                WorkflowModel().interpret({},'同意')
        with patch('urllib.request.build_opener') as opener:
            request=__import__('urllib.request',fromlist=['Request']).Request('https://example.test/v1')
            WorkflowModel._open(request)
            opener.return_value.open.assert_called_once_with(request,timeout=90)

    def test_non_json_envelope_missing_content_and_truncation_have_separate_errors(self):
        cases=[(b'<html>gateway error</html>','响应不是有效 JSON'),
               (b'{}','缺少模型回复字段'),
               (json.dumps({'choices':[{'message':{'content':''},'finish_reason':'length'}]}).encode(),'被平台截断'),
               (json.dumps({'choices':[{'message':{},'finish_reason':'length'}]}).encode(),'被平台截断'),
               (json.dumps({'choices':[{'message':{'content':''}}]}).encode(),'空内容')]
        for body,reason in cases:
            with self.subTest(reason=reason),self.assertRaisesRegex(ValueError,reason):
                self.interpret_body(body)

    def test_single_fenced_json_is_accepted_but_extra_prose_is_not(self):
        text=json.dumps({'action':'accept','confidence':.95,'reason':'明确同意'})
        body=json.dumps({'choices':[{'message':{'content':'```json\n'+text+'\n```'}}]}).encode()
        self.assertEqual(self.interpret_body(body)['action'],'accept')
        body=json.dumps({'choices':[{'message':{'content':'解释文字 '+text}}]}).encode()
        with self.assertRaisesRegex(ValueError,'不是完整的 JSON 判断'):
            self.interpret_body(body)

    def test_429_does_not_imply_quota_failure_without_explicit_provider_reason(self):
        from zhishi.models.workflow_errors import http_reason
        cases=[({},'不能据此认定余额不足'),
               ({'error':{'type':'rate_limit_error','message':'Too many requests'}},'暂时限流'),
               ({'error':{'code':'no_available_channel','message':'overloaded'}},'暂时繁忙'),
               ({'error':{'code':'insufficient_quota','message':'quota exhausted'}},'明确返回额度或余额不足')]
        for payload,reason in cases:
            with self.subTest(reason=reason):
                error=urllib.error.HTTPError('https://example.test/v1',429,'rate limited',{'Retry-After':'20'},io.BytesIO(json.dumps(payload).encode()))
                result=http_reason(error)
                self.assertIn(reason,result)

    def test_429_classification_does_not_expose_raw_provider_body(self):
        from zhishi.models.workflow_errors import http_reason
        body=json.dumps({'error':{'code':'rate_limit_error','message':'dummy-secret-token account-private-diagnostic'}}).encode()
        error=urllib.error.HTTPError('https://example.test/v1',429,'rate limited',{},io.BytesIO(body))
        result=http_reason(error)
        self.assertNotIn('dummy-secret-token',result)
        self.assertNotIn('account-private-diagnostic',result)


if __name__ == '__main__':
    unittest.main()

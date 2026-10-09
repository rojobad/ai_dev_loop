"""Evidence checks and offline SDK transport contracts; no live model calls."""
import json
import runpy
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from cursor_sdk import CursorClient, AgentOptions, LocalAgentOptions
from cursor_sdk.errors import AuthenticationError, RateLimitError, InternalServerError, APITimeoutError

ROOT=Path(__file__).parent
def load(name):return json.loads((ROOT/(name+'.json')).read_text())

class EvidenceTests(unittest.TestCase):
    def test_exact_initial_token_evidence(self):
        self.assertEqual(load('first')['usage'],{'input_tokens':4820,'output_tokens':139,'cache_read_tokens':2560,'cache_write_tokens':0,'total_tokens':7519,'reasoning_tokens':118})
    def test_resume_preserves_identity_and_memory(self):
        self.assertEqual(load('first')['agent_id'],load('resume')['agent_id'])
        self.assertIn('ORCHID-731',load('resume')['result'])
        self.assertEqual(load('first')['model'],load('resume')['model'])
    def test_rules_and_skill_are_applied(self):
        self.assertIn('POC-RULE-905',load('first')['result'])
        self.assertIn('POC-SKILL-472',load('tools')['result'])
        self.assertIn('POC-RULE-905',load('tools')['result'])
    def test_edit_correctness_independent_of_model_answer(self):
        add=runpy.run_path(str(ROOT/'workspace/calc.py'))['add']
        for a,b,expected in [(2,3,5),(-3,2,-1),(0,0,0),(100,-100,0)]:
            self.assertEqual(add(a,b),expected)
    def test_live_and_terminal_usage_agree_without_reasoning_double_count(self):
        for label in ['first','resume','tools','mcp','cancel-resume']:
            record=load(label);usage=record['usage']
            self.assertEqual(record['live_usage'],usage)
            self.assertEqual(sum(usage[k] for k in ['input_tokens','output_tokens','cache_read_tokens','cache_write_tokens']),usage['total_tokens'])
            self.assertEqual(len(record['usage_events']),1)
            self.assertEqual(record['usage_events'][0]['usage'],usage)
    def test_durable_run_snapshots_preserve_usage_and_results(self):
        for result in load('inspect'):
            self.assertTrue(result['same_run_id'])
            self.assertTrue(result['result_matches'])
            self.assertTrue(result['usage_matches'])
            self.assertEqual(result['conversation_turn_count'],1)
    def test_replay_has_usage_but_must_not_be_added_again(self):
        for label in ['first','resume']:
            messages=[e['sdk_message'] for e in load(label+'-replay') if e.get('sdk_message')]
            totals=[m['usage']['total_tokens'] for m in messages if m.get('type')=='usage']
            self.assertEqual(totals,[load(label)['usage']['total_tokens']])
    def test_cancel_stops_owned_shell_and_marks_usage_absent(self):
        result=load('cancel')
        self.assertTrue(result['tool_started'])
        self.assertEqual(result['status'],'cancelled')
        self.assertTrue(result['stream_finished'])
        self.assertFalse(result['child_alive_after_cancel'])
        self.assertFalse(result['finished_marker_exists'])
        self.assertFalse(result['poc_child_cleanup_required'])
        self.assertIsNone(result['usage'])
    def test_cancelled_conversation_resumes(self):
        self.assertEqual(load('cancel-run')['agent_id'],load('cancel-resume')['agent_id'])
        self.assertIn('POC-CANCEL-616',load('cancel-resume')['result'])
    def test_project_mcp_called_once(self):
        calls=[json.loads(line) for line in (ROOT/'mcp-calls.ndjson').read_text().splitlines()]
        self.assertEqual(calls,[{'name':'poc_probe'}])
        self.assertIn('POC-MCP-281',load('mcp')['result'])
    def test_billing_unavailability_is_explicit(self):
        self.assertEqual(load('billed-usage')['error_class'],'InternalServerError')
        self.assertIn('feature_unavailable',load('billed-usage')['message'])
    def test_real_bad_auth_and_unknown_identity_fail(self):
        records={r['case']:r for r in load('negative')}
        self.assertEqual(records['invalid_auth']['error_class'],'AuthenticationError')
        self.assertEqual(records['missing_agent']['error_class'],'AgentNotFoundError')
    def test_custom_callback_is_invoked_exactly_once(self):
        self.assertIn('POC-CUSTOM-364',load('custom')['result'])
        self.assertEqual(load('custom-calls'),[{'args':{},'has_tool_call_id':True}])
        self.assertEqual(load('custom-calls-without-mcp-tools'),[])
    def test_replay_cursor_returns_exact_suffix(self):
        result=load('replay-cursor')
        self.assertTrue(result['suffix_exact'])
        self.assertTrue(result['all_offsets_unique'])
        self.assertEqual(result['all_count']-5,result['suffix_count'])
    def test_actual_runtime_crash_requires_process_cleanup(self):
        result=load('runtime-crash')
        self.assertTrue(result['tool_started'])
        self.assertEqual(result['killed_pid'],result['runtime_pid'])
        self.assertTrue(result['child_alive_after_bridge_sigkill'])
        self.assertTrue(result['poc_child_cleanup_required'])
        self.assertTrue(result['stream_finished'])
        self.assertEqual(result['consumer_errors'][0]['error_class'],'NetworkError')
        self.assertEqual(load('runtime-crash-reopened-snapshot')['status'],'running')
        self.assertIsNone(result['usage'])
    def test_registered_cancel_clears_stale_run_and_preserves_identity(self):
        result=load('runtime-crash-cancel')
        self.assertTrue(result['attempts'][0]['success'])
        self.assertEqual(result['status_after_cancel'],'cancelled')
        self.assertEqual(load('runtime-crash-run-id')['agent_id'],load('runtime-crash-recovered')['agent_id'])
        self.assertIn('POC-CRASH-852',load('runtime-crash-recovered')['result'])
    def test_launcher_pid_is_not_runtime_pid(self):
        result=load('process-topology')
        self.assertNotEqual(result['launcher_pid'],result['endpoint_pid'])
        self.assertIn('bridge/bin/node',result['endpoint_pid_command'])
    def test_project_agents_instructions_are_applied(self):
        self.assertIn('POC-AGENTS-193',load('metadata')['result'])
        self.assertIn('POC-RULE-905',load('metadata')['result'])
    def test_strict_key_guard_rejects_local_replay_in_this_sdk_version(self):
        for result in load('inspect-strict'):
            self.assertTrue(result['usage_matches'])
            self.assertIn('ConfigurationError',result['observe_error'])
            self.assertIn('missing_api_key',result['conversation_error'])
    def test_supports_observe_is_false_despite_working_replay(self):
        for result in load('inspect'):
            self.assertFalse(result['supports']['observe'])
            self.assertGreater(result['observed_envelopes'],0)
    def test_historical_fixtures_read_without_cli_or_byte_changes(self):
        for result in load('historical-read'):
            self.assertTrue(result['unchanged_bytes'])
            self.assertFalse(result['cli_used'])
        self.assertEqual(load('historical-read')[1]['validated_event_count'],14)
    def test_explicit_catalog_parameters_are_frozen_across_turns(self):
        expected={'id':'grok-4.7','params':[{'id':'context','value':'256k'},{'id':'reasoning_effort','value':'high'},{'id':'fast','value':'false'}]}
        for label in ('first','resume','tools','metadata','runtime-crash-recovered'):
            self.assertEqual(load(label)['model'],expected)

class OfflineTransportTests(unittest.TestCase):
    def client(self,handler,retries=0):
        http=httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(http.close)
        client=CursorClient.connect('http://isolated-poc.invalid','fake-bridge-token',http_client=http,max_retries=retries)
        self.addCleanup(client.close)
        return client
    def test_401_is_typed_and_not_retried_by_default(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(401,json={'code':'unauthenticated','message':'synthetic bad key'})
        client=self.client(handler)
        with self.assertRaises(AuthenticationError):client.me(api_key='crsr_fake')
        self.assertEqual(len(calls),1)
    def test_rate_limit_preserves_retry_after_and_request_id(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(429,json={'code':'resource_exhausted','message':'synthetic capacity limit','details':[{'sdkErrorCode':'SDK_ERROR_CODE_USAGE_LIMIT_EXCEEDED','retryAfter':'10','requestId':'poc-request-429'}]})
        client=self.client(handler)
        with self.assertRaises(RateLimitError) as caught:client.models.list(api_key='crsr_fake')
        self.assertEqual(caught.exception.retry_after,'10')
        self.assertEqual(caught.exception.request_id,'poc-request-429')
        self.assertEqual(len(calls),1)
    def test_read_only_catalog_can_retry_when_explicitly_enabled(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            if len(calls)==1:return httpx.Response(503,json={'code':'unavailable','message':'synthetic transient'})
            return httpx.Response(200,json={'items':[{'id':'grok-4.7','displayName':'Grok 4.7'}]})
        client=self.client(handler,retries=2)
        with patch('cursor_sdk._connect._sleep_before_retry',return_value=None):models=client.models.list(api_key='crsr_fake')
        self.assertEqual([m.id for m in models],['grok-4.7'])
        self.assertEqual(len(calls),2)
    def test_mutating_create_is_never_retried_by_transport(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            return httpx.Response(503,json={'code':'internal','message':'synthetic create uncertainty'})
        client=self.client(handler,retries=2)
        with self.assertRaises(InternalServerError):
            client.agents.create(AgentOptions(model='grok-4.7',api_key='crsr_fake',local=LocalAgentOptions(cwd=ROOT/'workspace')))
        self.assertEqual(len(calls),1)
    def test_read_timeout_is_typed_and_not_retried_by_default(self):
        calls=[]
        def handler(request):
            calls.append(request.url.path)
            raise httpx.ReadTimeout('synthetic timeout',request=request)
        client=self.client(handler)
        with self.assertRaises(APITimeoutError):client.models.list(api_key='crsr_fake')
        self.assertEqual(len(calls),1)

if __name__=='__main__':unittest.main(verbosity=2)

"""Isolated SDK proof; credentials are ephemeral environment input, never persisted."""
import dataclasses
import importlib.metadata
import json
import os
import re
import sys
import time
import threading
import signal
from pathlib import Path

ROOT = Path(__file__).parent
WORKSPACE = ROOT / 'workspace'
STATE = ROOT / 'state'
KEY = os.environ.pop('CURSOR_API_KEY', '')

def clean(value):
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not any(x in k.lower() for x in ('api_key', 'apikey', 'auth_token', 'email'))}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, str):
        if KEY:
            value = value.replace(KEY, '[REDACTED]')
        return re.sub(r'crsr_[A-Za-z0-9_-]+', '[REDACTED]', value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return clean(str(value))

def emit(value):
    print(json.dumps(clean(value), ensure_ascii=False), flush=True)

def save(name, value):
    (ROOT / (name + '.json')).write_text(json.dumps(clean(value), indent=2, ensure_ascii=False) + '\n')

def bridge():
    from cursor_sdk import CursorClient, LocalAgentOptions, LocalAgentStoreConfig
    return CursorClient.launch_bridge(
        workspace=WORKSPACE, state_root=STATE,
        local=LocalAgentOptions(cwd=WORKSPACE, setting_sources=['project'],
            store=LocalAgentStoreConfig(type='jsonl', root_dir=str(STATE / 'agents'))),
        timeout=30, client_timeout=60, max_retries=0,
        allow_api_key_env_fallback=(len(sys.argv)<2 or sys.argv[1] != 'inspect-strict'),
    )

def options(model):
    from cursor_sdk import AgentOptions, LocalAgentOptions, LocalAgentStoreConfig
    if model == 'grok-4.7-high':
        model = {'id':'grok-4.7','params':[{'id':'context','value':'256k'},{'id':'reasoning_effort','value':'high'},{'id':'fast','value':'false'}]}
    return AgentOptions(model=model, api_key=KEY, name='isolated-sdk-adoption-poc', tools=[],
        local=LocalAgentOptions(cwd=WORKSPACE, setting_sources=['project'],
            store=LocalAgentStoreConfig(type='jsonl', root_dir=str(STATE / 'agents'))))

def record_run(run, label):
    events=[]; envelopes=[]
    started=time.monotonic()
    for envelope in run.events():
        envelopes.append(clean(envelope))
        message=envelope.sdk_message
        if message is None:
            continue
        event=clean(message)
        events.append(event)
        emit({'phase':label,'event_type':getattr(message,'type',None)})
    result=run.wait()
    record={'run_id':run.id, 'agent_id':run.agent_id, 'status':result.status,
        'result':result.result, 'usage':clean(result.usage), 'live_usage':clean(run.usage),
        'duration_seconds':round(time.monotonic()-started,3), 'model':clean(getattr(result,'model',None)),
        'event_types':[e.get('type') for e in events],
        'usage_events':[e for e in events if e.get('type')=='usage']}
    save(label+'-events',events); save(label+'-envelopes',envelopes); save(label,record); emit(record)
    return record

def main():
    mode=sys.argv[1]
    WORKSPACE.mkdir(exist_ok=True); STATE.mkdir(exist_ok=True)
    from cursor_sdk import Agent
    emit({'mode':mode,'sdk_version':importlib.metadata.version('cursor-sdk'),'credential_not_in_child_environment':'CURSOR_API_KEY' not in os.environ})
    with bridge() as client:
        emit({'bridge_version':client.get_version(),'bridge_health':client.ping()})
        if mode=='preflight':
            client.me(api_key=KEY)
            models=client.models.list(api_key=KEY)
            record={'authentication':'ok','models':[clean(m) for m in models]}
            save('preflight',record)
            emit({'authentication':'ok','model_ids':[m.id for m in models]})
        elif mode=='first':
            model=sys.argv[2]
            with Agent.create(options(model), client=client) as agent:
                save('identity',{'agent_id':agent.agent_id,'model':model})
                record_run(agent.send('Remember the private test marker ORCHID-731 for our next message. Acknowledge with one short sentence and comply with the project rules.'),'first')
                emit({'history_message_count':len(agent.list_messages())})
        elif mode=='resume':
            identity=json.loads((ROOT/'identity.json').read_text())
            with Agent.resume(identity['agent_id'],options(identity['model']),client=client) as agent:
                emit({'same_agent_id':agent.agent_id==identity['agent_id'],'history_before_send':len(agent.list_messages())})
                result=record_run(agent.send('What exact marker did I ask you to remember in my previous message? Answer briefly and comply with project rules.'),'resume')
                emit({'memory_preserved':'ORCHID-731' in result['result'],'project_rule_observed':'POC-RULE-905' in result['result']})
                try:
                    usage=agent.get_usage()
                    save('billed-usage',usage);emit({'billed_usage':clean(usage)})
                except Exception as exc:
                    save('billed-usage',{'error_class':type(exc).__name__,'message':clean(str(exc))[:1500]})
                    emit({'billed_usage_error_class':type(exc).__name__,'message':clean(str(exc))[:400]})
        elif mode in ('inspect','inspect-strict'):
            identity=json.loads((ROOT/'identity.json').read_text())
            records=[]
            for label in ['first','resume']:
                expected=json.loads((ROOT/(label+'.json')).read_text())
                run=client.agents.get_run(expected['run_id'], {'runtime':'local','agentId':identity['agent_id'],'apiKey':KEY})
                record={'label':label,'same_run_id':run.id==expected['run_id'], 'status':run.status,
                    'result_matches':run.result==expected['result'], 'usage':clean(run.usage),
                    'usage_matches':clean(run.usage)==expected['usage'],
                    'supports':{op:run.supports(op) for op in ['wait','observe','cancel','conversation']}}
                try:
                    record['conversation_turn_count']=len(run.conversation())
                except Exception as exc:
                    record['conversation_error']=type(exc).__name__+': '+clean(str(exc))[:300]
                try:
                    observed=list(run.observe())
                    record['observed_envelopes']=len(observed)
                    record['observed_usage_messages']=sum(1 for e in observed if getattr(e.sdk_message,'type',None)=='usage')
                    save(label+'-replay',observed)
                except Exception as exc:
                    record['observe_error']=type(exc).__name__+': '+clean(str(exc))[:300]
                records.append(record);emit(record)
            save(mode,records)
        elif mode=='negative':
            records=[]
            calls=[('invalid_auth',lambda:client.me(api_key='crsr_invalid_poc_key')),
                ('missing_agent',lambda:Agent.resume('agent-00000000-0000-0000-0000-000000000000',options('grok-4.7-high'),client=client))]
            for label,call in calls:
                try:
                    obj=call();record={'case':label,'unexpected_success':True,'object_type':type(obj).__name__}
                    if isinstance(obj,Agent):obj.close()
                except Exception as exc:
                    record={'case':label,'error_class':type(exc).__name__,'message':clean(str(exc))[:700]}
                records.append(record);emit(record)
            save('negative',records)
        elif mode=='tools':
            opt=dataclasses.replace(options('grok-4.7-high'),tools=['read','edit','ls','glob','grep'])
            with Agent.create(opt,client=client) as agent:
                save('tools-identity',{'agent_id':agent.agent_id,'model':'grok-4.7-high'})
                record_run(agent.send('Use the project skill poc-adoption. Fix calc.py so add(a,b) adds its two arguments. Follow the skill and project rules. Only modify calc.py in this workspace, use no shell or subagents, and give a brief final response.'),'tools')
        elif mode=='cancel':
            from cursor_sdk import SandboxOptions
            base=options('grok-4.7-high')
            opt=dataclasses.replace(base,tools=['shell'],local=dataclasses.replace(base.local,sandbox_options=SandboxOptions(enabled=True)))
            with Agent.create(opt,client=client) as agent:
                save('cancel-identity',{'agent_id':agent.agent_id,'model':'grok-4.7-high'})
                run=agent.send('Remember test marker POC-CANCEL-616. Run exactly python3 slow.py in this workspace once using the shell tool. The test controller will cancel it. Do not inspect anything else, do not retry it, and do not use subagents.')
                errors=[]
                def consume():
                    try:record_run(run,'cancel-run')
                    except Exception as exc:errors.append({'error_class':type(exc).__name__,'message':clean(str(exc))[:700]})
                worker=threading.Thread(target=consume,daemon=True);worker.start()
                deadline=time.monotonic()+40
                marker=WORKSPACE/'slow.started'
                while not marker.exists() and worker.is_alive() and time.monotonic()<deadline:
                    time.sleep(.05)
                began=marker.exists()
                pid=int(marker.read_text()) if began else None
                started=time.monotonic()
                try:
                    run.cancel();cancel_error=None
                except Exception as exc:
                    cancel_error={'error_class':type(exc).__name__,'message':clean(str(exc))[:700]}
                worker.join(15)
                def own_child_alive():
                    if pid is None:return False
                    try:
                        command=Path('/proc/'+str(pid)+'/cmdline').read_bytes()
                        return b'slow.py' in command
                    except FileNotFoundError:return False
                deadline=time.monotonic()+5
                while own_child_alive() and time.monotonic()<deadline:time.sleep(.1)
                alive=own_child_alive()
                cleanup=False
                if alive:
                    os.kill(pid,signal.SIGTERM);cleanup=True
                record={'run_id':run.id,'tool_started':began,'status':run.status,'cancel_error':cancel_error,
                    'stream_finished':not worker.is_alive(),'consumer_errors':errors,
                    'child_alive_after_cancel':alive,'poc_child_cleanup_required':cleanup,
                    'finished_marker_exists':(WORKSPACE/'slow.finished').exists(),
                    'cancel_elapsed_seconds':round(time.monotonic()-started,3),'usage':clean(run.usage)}
                save('cancel',record);emit(record)
                if worker.is_alive():raise RuntimeError('SDK stream did not terminate after cancellation')
        elif mode=='cancel-resume':
            identity=json.loads((ROOT/'cancel-identity.json').read_text())
            with Agent.resume(identity['agent_id'],options(identity['model']),client=client) as agent:
                record_run(agent.send('What exact test marker was included in my previous request before cancellation? Do not use tools or rerun the command. Give a brief answer.'),'cancel-resume')
        elif mode=='mcp':
            opt=dataclasses.replace(options('grok-4.7-high'),tools=['mcp','getMcpTools'])
            with Agent.create(opt,client=client) as agent:
                record_run(agent.send('Call poc_probe from the project-configured MCP server poc exactly once. Return its exact text. Do not use any other tools, do not create subagents, and comply with project rules.'),'mcp')
        elif mode=='custom':
            from cursor_sdk import CustomTool
            calls=[]
            def execute(args,context):
                calls.append({'args':clean(args),'has_tool_call_id':bool(context.tool_call_id)})
                return {'content':[{'type':'text','text':'POC-CUSTOM-364'}]}
            base=options('grok-4.7-high')
            tool=CustomTool(execute=execute,description='Returns the isolated test sentinel. Call once when requested.',input_schema={'type':'object','properties':{},'additionalProperties':False})
            opt=dataclasses.replace(base,tools=['mcp','getMcpTools'],local=dataclasses.replace(base.local,custom_tools={'poc_custom':tool}))
            with Agent.create(opt,client=client) as agent:
                record_run(agent.send('Call the custom tool poc_custom exactly once and return its exact text. Do not use any other tools or subagents. Comply with project rules.'),'custom')
            save('custom-calls',calls);emit({'custom_call_count':len(calls)})
        elif mode in ('crash','runtime-crash'):
            from cursor_sdk import SandboxOptions
            base=options('grok-4.7-high')
            opt=dataclasses.replace(base,tools=['shell'],local=dataclasses.replace(base.local,sandbox_options=SandboxOptions(enabled=True)))
            agent=Agent.create(opt,client=client)
            label=mode
            script='runtimecrash.py' if mode=='runtime-crash' else 'crash.py'
            prefix='runtimecrash' if mode=='runtime-crash' else 'crash'
            save(label+'-identity',{'agent_id':agent.agent_id,'model':'grok-4.7-high'})
            run=agent.send('Remember marker POC-CRASH-852. Run exactly python3 '+script+' in this workspace once using shell. The test controller will interrupt the runtime. Do not retry, inspect anything else, or use subagents.')
            errors=[]
            def consume():
                try:record_run(run,label+'-run')
                except Exception as exc:errors.append({'error_class':type(exc).__name__,'message':clean(str(exc))[:700]})
            worker=threading.Thread(target=consume,daemon=True);worker.start()
            marker=WORKSPACE/(prefix+'.started');deadline=time.monotonic()+40
            while not marker.exists() and worker.is_alive() and time.monotonic()<deadline:time.sleep(.05)
            began=marker.exists();pid=int(marker.read_text()) if began else None
            busy=None
            try:
                duplicate=agent.send('Reply with BUSY-CHECK only. Do not use tools.')
                busy={'unexpected_second_run':duplicate.id}
                duplicate.cancel()
            except Exception as exc:
                busy={'error_class':type(exc).__name__,'message':clean(str(exc))[:700]}
            save(label+'-run-id',{'run_id':run.id,'agent_id':agent.agent_id})
            started=time.monotonic()
            launcher_pid=client._owned_bridge.process.pid
            runtime_pid=client._owned_bridge.endpoint.pid
            killed_pid=runtime_pid if mode=='runtime-crash' else launcher_pid
            os.kill(killed_pid,signal.SIGKILL)
            worker.join(10)
            def alive():
                if pid is None:return False
                try:return script.encode() in Path('/proc/'+str(pid)+'/cmdline').read_bytes()
                except FileNotFoundError:return False
            deadline=time.monotonic()+3
            while alive() and time.monotonic()<deadline:time.sleep(.1)
            child_alive=alive();cleanup=False
            if child_alive:os.kill(pid,signal.SIGTERM);cleanup=True
            record={'tool_started':began,'run_id':run.id,'last_observed_status':run.status,
                'launcher_pid':launcher_pid,'runtime_pid':runtime_pid,'killed_pid':killed_pid,
                'second_send_while_busy':busy,'stream_finished':not worker.is_alive(),'consumer_errors':errors,
                'child_alive_after_bridge_sigkill':child_alive,'poc_child_cleanup_required':cleanup,
                'finished_marker_exists':(WORKSPACE/(prefix+'.finished')).exists(),
                'elapsed_after_kill_seconds':round(time.monotonic()-started,3),'usage':clean(run.usage)}
            save(label,record);emit(record)
        elif mode in ('crash-inspect','runtime-crash-inspect'):
            label=mode.replace('-inspect','')
            identity=json.loads((ROOT/(label+'-identity.json')).read_text())
            previous=json.loads((ROOT/(label+'-run-id.json')).read_text())
            run=client.agents.get_run(previous['run_id'],{'runtime':'local','agentId':identity['agent_id'],'apiKey':KEY})
            snapshot={'run_id':run.id,'status':run.status,'result':run.result,'usage':clean(run.usage)}
            save(label+'-reopened-snapshot',snapshot);emit(snapshot)
            with Agent.resume(identity['agent_id'],options(identity['model']),client=client) as agent:
                emit({'crash_resume_same_agent':agent.agent_id==identity['agent_id']})
                record_run(agent.send('What exact test marker was included in my previous request before interruption? Do not use tools or rerun the command. Give a brief answer.'),label+'-resume')
        elif mode=='runtime-crash-cancel':
            identity=json.loads((ROOT/'runtime-crash-identity.json').read_text())
            previous=json.loads((ROOT/'runtime-crash-run-id.json').read_text())
            run=client.agents.get_run(previous['run_id'],{'runtime':'local','agentId':identity['agent_id'],'apiKey':KEY})
            attempts=[]
            try:
                run.cancel();attempts.append({'operation':'registered_run_cancel','success':True})
            except Exception as exc:
                attempts.append({'operation':'registered_run_cancel','error_class':type(exc).__name__,'message':clean(str(exc))[:900]})
            with bridge() as detached:
                try:
                    detached.agents.cancel_run(previous['run_id'])
                    attempts.append({'operation':'detached_local_cancel_no_agent_id','success':True})
                except Exception as exc:
                    attempts.append({'operation':'detached_local_cancel_no_agent_id','error_class':type(exc).__name__,'message':clean(str(exc))[:900]})
            snapshot=client.agents.get_run(previous['run_id'],{'runtime':'local','agentId':identity['agent_id'],'apiKey':KEY})
            record={'attempts':attempts,'status_after_cancel':snapshot.status,'usage':clean(snapshot.usage)}
            save('runtime-crash-cancel',record);emit(record)
            if snapshot.status!='running':
                with Agent.resume(identity['agent_id'],options(identity['model']),client=client) as agent:
                    record_run(agent.send('What marker was in the request that was interrupted? Do not use tools or rerun the command. Answer briefly.'),'runtime-crash-recovered')
        elif mode=='replay-cursor':
            expected=json.loads((ROOT/'tools.json').read_text())
            run=client.agents.get_run(expected['run_id'],{'runtime':'local','agentId':expected['agent_id'],'apiKey':KEY})
            all_events=list(run.observe())
            cursor=all_events[4].offset
            suffix=list(run.observe(after_offset=cursor))
            record={'cursor':cursor,'all_count':len(all_events),'suffix_count':len(suffix),
                'suffix_exact':clean(suffix)==clean(all_events[5:]),
                'first_suffix_offset':suffix[0].offset if suffix else None,
                'all_offsets_unique':len({e.offset for e in all_events})==len(all_events)}
            save('replay-cursor',record);emit(record)
        elif mode=='metadata':
            with Agent.create(options('grok-4.7-high'),client=client) as agent:
                record_run(agent.send('Reply with one short acknowledgement. Follow all project instructions.'),'metadata')


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        error={'mode':sys.argv[1] if len(sys.argv)>1 else None,'error_class':type(exc).__name__,'message':clean(str(exc))[:2500]}
        save('error-'+str(error['mode']),error);emit(error)
        sys.exit(1)

"""Synthetic local MCP server. No remote integrations or credentials."""
import json
import os
import sys
from pathlib import Path
ROOT=Path(__file__).parent
(ROOT/'mcp.pid').write_text(str(os.getpid()))
for line in sys.stdin:
    try:message=json.loads(line)
    except ValueError:continue
    if 'id' not in message:continue
    method=message.get('method')
    if method=='initialize':
        result={'protocolVersion':message.get('params',{}).get('protocolVersion','2024-11-05'),'capabilities':{'tools':{}},'serverInfo':{'name':'isolated-poc','version':'1.0'}}
    elif method=='tools/list':
        result={'tools':[{'name':'poc_probe','description':'Return the isolated SDK adoption test sentinel.','inputSchema':{'type':'object','properties':{},'additionalProperties':False}}]}
    elif method=='tools/call':
        with (ROOT/'mcp-calls.ndjson').open('a') as f:f.write(json.dumps({'name':message.get('params',{}).get('name')})+'\n')
        result={'content':[{'type':'text','text':'POC-MCP-281'}],'isError':False}
    elif method=='ping':result={}
    elif method=='resources/list':result={'resources':[]}
    elif method=='prompts/list':result={'prompts':[]}
    else:
        print(json.dumps({'jsonrpc':'2.0','id':message['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True);continue
    print(json.dumps({'jsonrpc':'2.0','id':message['id'],'result':result}),flush=True)

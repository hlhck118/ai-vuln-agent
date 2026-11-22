import os, sys, tempfile, json
ROOT = r'F:/Desktop/ai-vuln-agent'
sys.path.insert(0, ROOT)
d = tempfile.mkdtemp()
os.environ['PENTEST_TOOLS_CONFIG'] = os.path.join(d, 'tools.json')
from pentestd import server, store as store_mod, chat, artex
import tests.support as support
td, st = support.temporary_store('fix.db')
plane = server.ControlPlane(st, os.path.join(ROOT,'web'), require_executor_auth=False)
plane.data_dir = d
eid = st.create_engagement('10.0.0.9', 'fix', 'w')['id']
r1 = chat.execute_tool(plane,'create_mcp',{'name':'demo2','transport':'stdio','command':'node','args':['x.js'],'label':'演示','level':'probe'},eid,policy_id='full',role='admin')
print('create', r1['ok'], r1['summary'])
before = st.get_mcp_server('demo2')
print('before', before['command'], before['args'], before['timeout_s'])
r2 = chat.execute_tool(plane,'update_mcp',{'id':'demo2','label':'改了名字'},eid,policy_id='full',role='admin')
print('update', r2['ok'], r2['summary'])
after = st.get_mcp_server('demo2')
print('after', after['label'], repr(after['command']), after['args'], after['timeout_s'], after['transport'])
st.close(); td.cleanup()
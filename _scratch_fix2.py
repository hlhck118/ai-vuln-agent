import os, sys, tempfile
ROOT = r'F:/Desktop/ai-vuln-agent'
sys.path.insert(0, ROOT)
d = tempfile.mkdtemp(); os.environ['PENTEST_TOOLS_CONFIG'] = os.path.join(d,'tools.json')
from pentestd import server, chat
import tests.support as support
td, st = support.temporary_store('fix2.db')
plane = server.ControlPlane(st, os.path.join(ROOT,'web'), require_executor_auth=False)
plane.data_dir = d
eid = st.create_engagement('10.0.0.10','fix2','w')['id']
c = st.save_mcp_server({'id':'slow','label':'慢','transport':'stdio','command':'python','args':['-m','x'],'env':{'A':'1'},'level':'audit','timeout_s':30})
print('saved timeout', c['timeout_s'])
r = chat.execute_tool(plane,'update_mcp',{'id':'slow','label':'慢点'},eid,policy_id='full',role='admin')
a = st.get_mcp_server('slow')
print('update ok', r['ok'], 'timeout', a['timeout_s'], 'level', a['level'], 'env', a['env'], 'command', a['command'])
h = st.save_mcp_server({'id':'web1','label':'网页','transport':'http','url':'https://x.test/mcp','level':'probe'})
r2 = chat.execute_tool(plane,'update_mcp',{'id':'web1','label':'网页改'},eid,policy_id='full',role='admin')
b = st.get_mcp_server('web1')
print('http update ok', r2['ok'], 'url', b['url'], 'transport', b['transport'])
r3 = chat.execute_tool(plane,'update_mcp',{'id':'web1','transport':'stdio'},eid,policy_id='full',role='admin')
print('switch to stdio without command:', r3['ok'], r3['summary'])
st.close(); td.cleanup()
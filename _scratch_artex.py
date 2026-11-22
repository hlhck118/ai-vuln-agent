# -*- coding: utf-8 -*-
import os, sys, json, tempfile
ROOT = r'F:/Desktop/ai-vuln-agent'
sys.path.insert(0, ROOT)
_cfgdir = tempfile.mkdtemp()
os.environ['PENTEST_TOOLS_CONFIG'] = os.path.join(_cfgdir, 'tools.json')
from pentestd import chat, server, toolkit, policy
from pentestd.store import Store
from tests import support

directory, store = support.temporary_store('scratch.db')
plane = server.ControlPlane(store, os.path.join(ROOT, 'web'), require_executor_auth=False)
plane.data_dir = directory.name
eid = store.create_engagement('10.10.10.250', 'artex 自测', '工单 #47')['id']

def call(name, args=None, e='', policy_id='full', role='admin'):
    out = chat.execute_tool(plane, name, args or {}, e or eid, policy_id=policy_id, role=role)
    flag = 'OK ' if out['ok'] else 'ERR'
    print('###', flag, name, '|', out['summary'][:140])
    if not out['ok']:
        print('    FULL:', out['summary'][:500])
    return out

# --- 只读
call('graph_overview')
call('list_findings')
call('list_facts')
call('list_assets')
call('list_goals')
call('list_companies')
call('list_tasks')
call('list_llm_profiles')
call('get_task_graph')

# --- 写链路
i = call('add_intent', {'intents': [{'title': '扫目录'}]})
intent_id = i['data']['intents'][0]['id'] if i['ok'] else ''
print('intent', intent_id)
f = call('record_fact', {'detail': '80 端口跑着 nginx', 'intent_id': intent_id, 'confidence': 0.9})
print('fact', (f.get('data') or {}).get('facts'))
g = call('set_goals', {'text': '拿下后台'})
print('goals', (g.get('data') or {}).get('goals'))
c = call('add_task_scope', {'kind': 'allow', 'value': '10.0.0.0/24'})
print('constraint', (c.get('data') or {}).get('constraints'))
h = call('add_hint', {'text': '管理员口令在配置里'})
print('hint', (h.get('data') or {}).get('hints'))
a = call('insert_assets', {'assets': [{'type': 'subdomain', 'value': 'a.example.test'}, {'type': 'service', 'value': '443/tcp https'}]})
print('assets', (a.get('data') or {}).get('created'))

r = call('report_finding', {'summary': '后台未鉴权可读用户表', 'severity': 'high', 'vulnclass': 'authz', 'evidence': '请求返回 200 与全量用户', 'intent_id': intent_id})
fid = (r.get('data') or {}).get('finding', {}).get('id') or ''
print('finding', fid, (r.get('data') or {}).get('verified'))
r2 = call('report_finding', {'summary': '疑似目录遍历（仅静态）', 'severity': 'low', 'intent_id': intent_id})
fid2 = (r2.get('data') or {}).get('finding', {}).get('id') or ''
print('finding2', fid2, (r2.get('data') or {}).get('verified'))
call('update_finding_report', {'finding_id': fid, 'report': '补一段说明', 'severity': 'critical'})
call('suggest_finding_merge', {'finding_ids': [fid, fid2]})
call('search_finding_duplicates', {'finding_id': fid})
m = call('merge_finding_records', {'finding_ids': [fid, fid2], 'title': '后台鉴权缺失', 'reason': '同一处问题两次观测'})
print('merge', (m.get('data') or {}).get('keeper'))

t = store.record_traffic(method='GET', url='http://10.10.10.250/api/users', status=200, response_body='[{"id":1}]', engagement_id=eid)
call('traffic_search', {'host': '10.10.10.250'})
call('traffic_get', {'id': t})
call('traffic_blob', {'hash': t, 'side': 'response'})
call('bind_finding_traffic', {'finding_id': fid, 'traffic_refs': [t]})
call('get_finding_traffic', {'finding_id': fid, 'side': 'response'})
call('get_finding_retest_context', {'finding_id': fid})
call('record_finding_retest_result', {'finding_id': fid, 'verdict': 'reproduced', 'summary': '复现了', 'evidence': '同一请求仍返回 200'})

lf = call('list_findings')
groups = (lf.get('data') or {}).get('groups') or []
print('groups', [g['key'][:20] for g in groups])
if groups:
    call('get_finding_case', {'case_id': groups[0]['key']})
    call('update_finding_case_report', {'case_id': groups[0]['key'], 'version': 'v1', 'report': '对外正文', 'severity': 'high'})
    call('complete_finding_case_review', {'summary': '这一轮看完', 'decisions': [{'case_id': groups[0]['key'], 'verdict': 'reproduced', 'reason': '仍然成立'}]})

call('record_intelligence', {'kind': 'credential', 'title': 'admin 口令形态', 'content': '在 /config.php 里出现过，形态为 32 位十六进制', 'evidence': 'GET /config.php 返回 200', 'confidence': 'observed'})
call('prove_goal', {'goal_id': 'goal-1', 'evidence_id': fid, 'reason': '这条漏洞就是达成凭据'})
call('goal_met', {'reason': '人工确认'})
call('kill_work', {'intent_id': intent_id})
call('steer_work', {'intent_id': intent_id, 'message': '先看登录页'})
call('pause_task', {})
print('paused', store.engagement_paused(eid))
call('pause_task', {'paused': False})
print('paused', store.engagement_paused(eid))

# --- 技能
s = call('create_skill', {'name': 'My Probe', 'description': '当需要探测时', 'instructions': '第一步……'})
print('skill', (s.get('data') or {}).get('path'))
s2 = call('create_skill', {'name': 'My Probe', 'description': 'x', 'instructions': 'y'})
print('overwritten', (s2.get('data') or {}).get('overwritten'))
call('update_skill_file', {'name': 'My Probe', 'content': '新正文'})
call('update_skill_file', {'name': 'My Probe', 'file': '../escape.md', 'content': 'x'})
call('update_skill_file', {'name': '不存在的技能', 'content': 'x'})

# --- 平台注册
ct = call('create_custom_tool', {'key': 'myprobe', 'exec': {'binaries': ['myprobe']}, 'description': '自测工具'})
print('tool', (ct.get('data') or {}).get('count'), (ct.get('data') or {}).get('path'))
call('create_custom_tool', {'key': 'myprobe', 'exec': {'binaries': ['myprobe']}})
call('update_custom_tool', {'key': 'myprobe', 'description': '改了说明'})
call('update_custom_tool', {'key': 'nope', 'description': 'x'})
call('create_custom_tool', {'key': 'badclass', 'kind': 'exe', 'exec': {'binaries': ['x']}})

mc = call('create_mcp', {'name': 'demo', 'command': 'demo-mcp'})
print('mcp', (mc.get('data') or {}).get('id'), (mc.get('data') or {}).get('level'))
call('create_mcp', {'name': 'demo', 'command': 'demo-mcp'})
call('update_mcp', {'id': 'demo', 'label': '演示服务'})
call('update_mcp', {'id': 'nope'})
call('create_mcp', {'name': 'Bad Name', 'command': 'x'})

# --- 权限与归属
call('record_fact', {'detail': 'x'}, policy_id='readonly')
call('create_mcp', {'name': 'denied', 'command': 'x'}, policy_id='probe')
call('list_facts', {'task_id': 'E-00000000'})
call('list_facts', {'task_id': eid})

# --- spawn_task / company
sp = call('spawn_task', {'goal': '拿下子域', 'description': '子任务一', 'seed_first_intent': True})
print('spawn', (sp.get('data') or {}).get('taskId'))
call('add_company_scope', {'company': '示例公司', 'scope': 'example.test', 'reason': '授权书 #12'})
call('list_companies')

toolkit.reload()
store.close()
directory.cleanup()
print('DONE')
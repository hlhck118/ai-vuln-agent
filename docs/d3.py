# -*- coding: utf-8 -*-
"""4.数据模型设计、5.接口设计"""

BLOCKS = [
    ('h1', '4.数据模型设计'),
    ('h2', '4.1 领域模型'),
    ('p', '系统的数据流由一组可 JSON 序列化的领域对象承载。所有对象都设计了明确的必填字段与状态枚举，状态流转受流程约束，非法流转在代码层不可表达，从数据结构上排除"结论状态被随意改写"的可能。'),
    ('caption', '表4-1 核心领域对象'),
    ('table', ['对象', '关键字段', '说明'],
     [
         ['Target', 'name、root、languages、file_count、loc、hash', '扫描目标描述，hash 为内容指纹'],
         ['EntryPoint', 'symbol、path、line、kind、params', '外部输入入口，kind 取值 function/route/main/module'],
         ['Sink', 'symbol、path、line、cwe、snippet、tainted_params', '危险汇聚点，是假设的静态来源'],
         ['CodeSlice', 'slice_id、path、symbol、start_line、end_line、code、graph_context、risk_score', '送审切片，一次推理的最小上下文单元'],
         ['GraphSummary', 'functions、calls、entry_points、sinks、call_edges、reachable_from_entry', '代码语义图谱摘要'],
         ['Hypothesis', 'hyp_id、slice_id、cwe、file、line、source、preconditions、confidence、severity_guess、rationale、evidence、status', '漏洞假设，status 为受控状态机'],
         ['Evidence', 'kind、ref、detail', '证据，kind 取值 static/graph/dynamic/memory/critic'],
         ['Verification', 'hyp_id、verdict、mode、poc_path、target_runs、observed、duration_ms', '验证结论与执行记录'],
         ['Finding', 'finding_id、cwe、title、severity、file、line、symbol、confidence、impact、remediation、evidence_chain、verification', '交付态漏洞条目'],
         ['AttackChain', 'chain_id、steps、narrative、severity', '攻击链及其关联交付项'],
         ['ScanResult', 'run_id、target、graph、slices、hypotheses、verifications、findings、chains、budget_snapshot、timeline、degraded', '一次任务的完整结果'],
     ], [15, 45, 40]),
    ('h2', '4.2 假设状态机'),
    ('p', '假设的生命周期是受控的：审计智能体产出时状态为 proposed；进入反证后转为 challenged；被反证推翻转为 dropped；通过差分验证转为 verified；无法自动判定转为 inconclusive。状态只能沿既定路径流转，且每次流转都要写入判定表，形成完整的判定轨迹。'),
    ('caption', '表4-2 假设状态流转'),
    ('table', ['当前状态', '触发条件', '下一状态', '是否进入交付清单'],
     [
         ['proposed', '生成阶段完成', 'challenged', '否'],
         ['challenged', '反证判定保留', '进入验证环节', '否'],
         ['challenged', '反证判定剔除', 'dropped', '否（保留备查）'],
         ['验证中', '差分验证成立', 'verified', '是'],
         ['验证中', '差分验证不成立', 'dropped', '否（保留备查）'],
         ['验证中', '无法构造或执行用例', 'inconclusive', '否（标注待人工确认）'],
         ['验证中', '无法执行但源码证据充分', '静态确认', '是（标注仅有源码证据）'],
         ['任意', '预算耗尽', 'inconclusive', '否（标注待人工确认）'],
     ], [16, 30, 22, 32]),
    ('h2', '4.3 记忆库表结构'),
    ('p', '记忆库采用标准库 SQLite，单文件存储、可直接备份与迁移，便于在离线环境中部署。表结构如下（与实现代码中的 DDL 完全一致）。'),
    ('code', 'CREATE TABLE runs (\n'
             '    run_id      TEXT PRIMARY KEY,\n'
             '    target_name TEXT,\n'
             '    target_hash TEXT,\n'
             '    started_at  REAL,\n'
             '    finished_at REAL,\n'
             '    stats_json  TEXT\n'
             ');\n'
             'CREATE TABLE hypotheses (\n'
             '    hyp_id     TEXT PRIMARY KEY,\n'
             '    run_id     TEXT,\n'
             '    cwe        TEXT,\n'
             '    title      TEXT,\n'
             '    file       TEXT,\n'
             '    symbol     TEXT,\n'
             '    line       INTEGER,\n'
             '    confidence REAL,\n'
             '    status     TEXT,\n'
             '    rationale  TEXT\n'
             ');\n'
             'CREATE TABLE verdicts (\n'
             '    hyp_id    TEXT, verdict TEXT, mode TEXT,\n'
             '    note      TEXT, evidence TEXT, created_at REAL\n'
             ');\n'
             'CREATE TABLE patterns (\n'
             '    cwe TEXT, signature TEXT, fix_hint TEXT,\n'
             '    hits INTEGER DEFAULT 0, first_seen REAL, last_seen REAL,\n'
             '    PRIMARY KEY (cwe, signature)\n'
             ');'),
    ('p', '索引方面，hypotheses 表按 cwe 建立索引以支撑同类缺陷检索，verdicts 表按 hyp_id 建立索引以支撑判定回溯。evidence 字段以 JSON 文本存储观测结果，长度上限 4000 字符，避免单条记录过大影响检索性能。'),
    ('h2', '4.4 结果文件结构'),
    ('p', '机读结果采用扁平化 JSON，字段命名与领域模型一致，便于缺陷平台与工单系统直接消费。核心结构如下。'),
    ('code', '{\n'
             '  "run_id": "R-78a85d2378",\n'
             '  "target": {"name": "vulnapp", "file_count": 1, "loc": 92,\n'
             '             "hash": "ffbd394da9df5551"},\n'
             '  "findings": [\n'
             '    {\n'
             '      "finding_id": "F-004",\n'
             '      "cwe": "CWE-89", "cwe_name": "SQL 注入",\n'
             '      "severity": "high", "confidence": 0.75,\n'
             '      "file": ".../userservice.py", "line": 40, "symbol": "find_user",\n'
             '      "impact": "攻击者可绕过查询条件读取、篡改或删除数据库中的任意数据……",\n'
             '      "remediation": "改用参数化查询（占位符绑定）……",\n'
             '      "evidence_chain": [{"kind": "static", "ref": "userservice.py:40",\n'
             '                          "detail": "SQL 语句由字符串拼接/格式化构造……"}],\n'
             '      "verification": {\n'
             '        "verdict": "verified", "mode": "dynamic",\n'
             '        "observed": {"effect_on_vulnerable": true,\n'
             '                     "effect_on_patched": false},\n'
             '        "target_runs": [{"target": "vulnerable", "exit_code": 0,\n'
             '                         "parsed": {"rows": 2}}]\n'
             '      }\n'
             '    }\n'
             '  ],\n'
             '  "chains": [{"chain_id": "C-4d2dd5", "severity": "critical",\n'
             '              "steps": ["F-001", "F-002", "F-003"]}]\n'
             '}'),

    ('h1', '5.接口设计'),
    ('h2', '5.1 命令行接口'),
    ('p', '命令行是系统的主要对外接口，既供人工使用，也供持续集成调用。所有子命令都支持通过 --config 指定配置文件，未指定时使用内置的离线保守配置。'),
    ('caption', '表5-1 命令行子命令'),
    ('table', ['子命令', '参数', '作用'],
     [
         ['scan', '--target、--patched、--out、--db、--json、--provider、--base-url、--model、--allow-cloud', '扫描目标并产出报告；--patched 指定修复版作为差分基线'],
         ['scan', '--incremental、--baseline、--git-ref', '增量扫描：只审变更文件及其影响面；基线默认写在输出目录，也可用 git 基准提交计算变更'],
         ['memory', '--db', '查看记忆库统计：任务数、假设数、已验证数、误报数、模式数'],
         ['doctor', '—', '环境自检：Python 版本、沙箱后端、容器运行时、模型配置、外部工具可用性'],
     ], [12, 52, 36]),
    ('p', '典型调用方式：扫描缺陷版并以修复版为基线，指定模型服务与输出目录。'),
    ('code', 'python -m vulnagent scan \\\n'
             '    --target  demo/vulnapp \\\n'
             '    --patched demo/vulnapp_patched \\\n'
             '    --provider openai_compat \\\n'
             '    --base-url http://127.0.0.1:8000/v1 \\\n'
             '    --model deepseek-r1-distill \\\n'
             '    --out out --db memory/cases.db'),
    ('p', '持续集成场景使用增量模式：首次运行建立基线，之后每次提交只审变更影响面；也可直接以某个提交为基准计算变更。'),
    ('code', 'python -m vulnagent scan --target ./repo --incremental \\\n'
             '    --baseline out/vulnagent-baseline.json\n'
             'python -m vulnagent scan --target ./repo --incremental --git-ref HEAD~1'),
    ('h2', '5.2 Python 编程接口'),
    ('p', '系统同时提供编程接口，便于嵌入既有平台或以库的形式在脚本中调用。接口只依赖三个对象：配置、编排器、结果。'),
    ('code', 'from vulnagent.config import load_config\n'
             'from vulnagent.orchestrator import Orchestrator\n'
             'from vulnagent.report import write_report\n'
             '\n'
             'cfg = load_config("config.toml")\n'
             'orch = Orchestrator(cfg, patched_root="demo/vulnapp_patched",\n'
             '                    memory_path="memory/cases.db")\n'
             'try:\n'
             '    result = orch.scan("demo/vulnapp")\n'
             '    paths = write_report(result, "out", orch.memory.stats())\n'
             '    for finding in result.findings:\n'
             '        print(finding.finding_id, finding.cwe, finding.severity,\n'
             '              finding.verification.verdict)\n'
             'finally:\n'
             '    orch.close()'),
    ('h2', '5.3 产物接口'),
    ('p', '每次任务产出四份文件，分别面向不同消费方。四份文件同源同批次，保证"报告看到的"与"接口拿到的"完全一致。'),
    ('caption', '表5-2 任务产物'),
    ('table', ['文件', '消费方', '内容'],
     [
         ['report.md', '安全工程师、复核人', '结论摘要、攻击链、漏洞清单、逐条详情（含证据链与复现记录）、执行轨迹'],
         ['findings.json', '缺陷平台、工单系统', '结构化交付项与攻击链，可直接映射为工单字段'],
         ['compliance.json', '合规与报送责任部门', '按监管要求整理的报送要素：产品、版本、漏洞类型、危害、影响范围、修复建议'],
         ['run_trace.json', '审计与复核', '预算快照、降级记录、智能体时间线、全部假设及其状态'],
     ], [18, 24, 58]),
    ('h2', '5.4 持续集成集成契约'),
    ('p', '在流水线场景中，系统以命令行方式被调用，通过退出码与结果文件与流水线交互。契约设计的原则是"默认不阻塞"：安全扫描不应因为工具自身故障而阻断业务发布。'),
    ('caption', '表5-3 流水线集成契约'),
    ('table', ['交互项', '约定'],
     [
         ['触发方式', '合并请求事件或每日定时构建触发，容器内执行'],
         ['输入', '增量代码目录或仓库地址；修复版基线可选'],
         ['执行时限', '单次不超过约定时间窗（建议 15 分钟），超时按已有结果输出'],
         ['退出码', '0 = 扫描完成（无论是否发现漏洞）；非 0 = 工具自身故障'],
         ['结果消费', '解析 findings.json，按严重程度决定"仅提示"或"阻断合并"，策略由使用方配置'],
         ['门禁建议', '严重与高危且状态为"已差分验证"的条目才纳入门禁；静态确认与待人工确认不阻断'],
         ['幂等性', '同一内容指纹重复扫描直接复用历史结论，不重复消耗算力'],
     ], [16, 84]),
    ('h2', '5.5 预留服务化接口'),
    ('p', '面向多团队共用的生产部署，系统预留 REST 接口设计。接口按"任务—结果—知识"三类资源组织，全部要求鉴权与操作审计；任务提交采用异步模式，避免长任务占用连接。接口设计在此固化，实现留待平台化阶段。'),
    ('caption', '表5-4 预留 REST 接口'),
    ('table', ['方法', '路径', '作用', '主要参数'],
     [
         ['POST', '/api/v1/scans', '提交扫描任务', 'target、patched、provider、budget_profile'],
         ['GET', '/api/v1/scans/{run_id}', '查询任务状态', '返回阶段、进度、预算消耗'],
         ['GET', '/api/v1/scans/{run_id}/findings', '获取交付项', 'severity、cwe、verdict 过滤'],
         ['GET', '/api/v1/scans/{run_id}/report', '获取报告', 'format=md|json'],
         ['GET', '/api/v1/scans/{run_id}/compliance', '获取合规要素', '—'],
         ['GET', '/api/v1/knowledge/similar', '相似案例检索', 'cwe、code_snippet'],
         ['POST', '/api/v1/knowledge/patterns', '提交修复模式', 'cwe、signature、fix_hint'],
         ['GET', '/api/v1/health', '健康检查', '返回模型可用性、沙箱后端、队列深度'],
     ], [10, 32, 26, 32]),
]

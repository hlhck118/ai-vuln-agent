# -*- coding: utf-8 -*-
"""附录A—附录D"""

PROMPT_AUDIT = (
    "你是资深代码安全审计专家，服务于授权范围内的漏洞发掘任务。"
    "你的结论必须可被证伪：给出漏洞位置、类型、成立所需前置条件与置信度。"
    "严禁编造代码中不存在的位置；没有证据支撑的判断必须放弃。"
    "只输出 JSON，不要输出任何解释性文字。"
)

PROMPT_CRITIC = (
    "你是安全审计的反证专家。你的任务是尽力推翻给定的漏洞假设："
    "检查前置条件是否可达、是否已有防护与校验、是否属于误报。"
    "只输出 JSON，不要输出任何解释性文字。"
)

PROMPT_SCHEMA = (
    '输出 JSON 数组，元素字段：'
    '{"cwe":"CWE-xx","title":"简短标题","line":整数行号,'
    '"source":"污点来源","preconditions":["条件1"],'
    '"confidence":0到1的小数,"severity":"low|medium|high|critical",'
    '"rationale":"判断依据，须引用具体代码"}。没有把握的条目不要输出。'
)

BLOCKS = [
    ('pagebreak',),
    ('h1', '附录A 目录结构与模块清单'),
    ('p', '原型代码遵循"一个关注点一个模块"的组织方式，模块划分与设计文档的分层架构严格对应。'),
    ('code', 'prototype/\n'
             '├── vulnagent/\n'
             '│   ├── domain.py        领域模型：假设、证据、验证、交付项、攻击链、扫描结果\n'
             '│   ├── config.py        配置加载：TOML/JSON，默认离线保守配置\n'
             '│   ├── resolver.py      符号解析与调用图：作用域、导入、轻量类型推断\n'
             '│   ├── analysis.py      规则引擎与切片构造：PyAnalyzer、ModuleUnit、切片构造\n'
             '│   ├── incremental.py   增量扫描：指纹清单、变更比对、影响面分析\n'
             '│   ├── llm.py           模型接入与推理器：ChatModel、RuleReasoner、LLMReasoner、Router\n'
             '│   ├── tools.py         工具白名单、沙箱执行（进程级/容器级）、8 类 PoC、差分验证\n'
             '│   ├── agents.py        七个智能体角色与共享黑板\n'
             '│   ├── orchestrator.py  编排引擎、预算控制、状态机、增量调度\n'
             '│   ├── memory.py        经验记忆库（SQLite）\n'
             '│   ├── report.py        Markdown 报告与合规要素\n'
             '│   └── cli.py           命令行入口（scan / memory / doctor）\n'
             '├── demo/vulnapp/          单文件目标：六类典型缺陷\n'
             '├── demo/webapp/           多文件目标：三层跨文件调用链\n'
             '├── demo/websvc/           Web 服务片段：XSS/代码注入/弱哈希/SSRF\n'
             '├── demo/*_patched/        各目标对应的修复版\n'
             '├── tests/test_pipeline.py 31 项自动化测试\n'
             '├── config.example.toml    配置示例\n'
             '├── run_demo.py            端到端演示脚本\n'
             '└── out/                   报告产物'),
    ('table', ['模块', '代码行数（约）', '核心类/函数', '职责'],
     [
         ['domain.py', '195', 'Target、Hypothesis、Finding、ScanResult', '领域模型与状态枚举'],
         ['config.py', '110', 'AppConfig、load_config', '配置加载与默认值'],
         ['resolver.py', '430', 'CallGraphIndex、CallGraph、build_call_graph', '符号解析与调用图构建'],
         ['analysis.py', '470', 'PyAnalyzer、ModuleUnit、build_slices', '规则引擎与切片构造'],
         ['incremental.py', '200', 'build_manifest、diff_manifests、impacted_units', '增量扫描与影响面分析'],
         ['llm.py', '330', 'RuleReasoner、LLMReasoner、Router、extract_json', '模型接入与降级'],
         ['tools.py', '470', 'ToolRegistry、SandboxRunner、DifferentialVerifier', '工具白名单、沙箱与差分验证'],
         ['agents.py', '320', 'ReconAgent…ReporterAgent、Blackboard', '七个智能体角色'],
         ['orchestrator.py', '210', 'Orchestrator、Budget、IncrementalPlan', '编排、预算与增量调度'],
         ['memory.py', '170', 'MemoryStore', '经验记忆库'],
         ['report.py', '250', 'render_markdown、build_compliance', '报告与合规输出'],
         ['cli.py', '135', 'cmd_scan、cmd_memory、cmd_doctor', '命令行接口'],
     ], [18, 14, 34, 34]),

    ('pagebreak',),
    ('h1', '附录B 配置项清单'),
    ('p', '配置文件采用 TOML（兼容 JSON），共六组配置。默认值即为"离线可跑通"的保守配置，未指定配置文件时全部使用默认值。'),
    ('table', ['配置组', '配置项', '默认值', '说明'],
     [
         ['run', 'output_dir', 'out', '报告输出目录'],
         ['run', 'languages', '.py/.java/.c/.cpp/.go/.js/.ts', '纳入分析的文件扩展名'],
         ['run', 'exclude_dirs', '.git/node_modules/dist/build/__pycache__/.venv/venv/target', '排除目录'],
         ['run', 'max_file_bytes', '524288', '单文件大小上限，超过则跳过'],
         ['model', 'provider', 'heuristic', 'heuristic 离线确定性 / openai_compat 接入模型'],
         ['model', 'base_url', 'http://127.0.0.1:8000/v1', '私有化模型服务的 OpenAI 兼容地址'],
         ['model', 'model', 'deepseek-r1-distill', '模型名称'],
         ['model', 'api_key_env', 'VULNAGENT_API_KEY', '密钥所在的环境变量名（不落配置文件）'],
         ['model', 'temperature_hypothesis / verdict', '0.7 / 0.2', '假设阶段求多样、判定阶段求稳定'],
         ['model', 'allow_cloud', 'false', '云端通道开关，默认关闭'],
         ['model', 'cloud_model', '空', '云端模型名；开启云端时必须填写，否则启动报错'],
         ['budget', 'max_rounds / max_tool_calls / max_tokens', '8 / 60 / 400000', '三重预算上限'],
         ['budget', 'max_slices / max_hypotheses_per_slice', '100 / 3', '切片与假设数量上限'],
         ['sandbox', 'backend', 'subprocess', 'subprocess 进程级 / docker 容器级'],
         ['sandbox', 'allow_fallback', 'true', '容器不可用时是否允许降级为进程级；生产必须为 false'],
         ['sandbox', 'timeout_s / memory_mb', '20 / 512', '执行超时与内存上限'],
         ['sandbox', 'allow_network', 'false', '沙箱是否允许出网'],
         ['tools', 'enabled', 'builtin_rules 等', '启用的工具清单'],
         ['tools', 'codeql_path / semgrep_path', '空', '外部静态分析工具路径，未配置则降级'],
         ['memory', 'enabled / db_path / top_k', 'true / memory/cases.db / 3', '记忆库开关、路径与召回条数'],
     ], [12, 30, 34, 24]),

    ('pagebreak',),
    ('h1', '附录C 关键提示词'),
    ('p', '提示词遵循三条原则：明确角色与边界、限定输出结构、要求证据引用。下面给出假设生成与反证质询两个阶段的系统提示词全文，以及输出结构约束。'),
    ('h3', '（1）假设生成阶段系统提示词'),
    ('code', PROMPT_AUDIT),
    ('h3', '（2）假设生成阶段输出结构约束（附于用户消息末尾）'),
    ('code', PROMPT_SCHEMA),
    ('h3', '（3）反证质询阶段系统提示词'),
    ('code', PROMPT_CRITIC),
    ('h3', '（4）反证质询阶段输出结构约束'),
    ('code', '输出 JSON 对象：{"keep":true/false,"confidence":0到1,'
             '"reason":"质询结论，须引用代码或防护证据","sanitizers":["已发现的防护"]}。'),
    ('p', '需要说明的是，提示词只负责引导输出形式，不承担安全责任。真正的约束在代码层：行号越界的假设会被回落或丢弃，无静态证据的假设在生成阶段即被剔除，最终结论一律以沙箱执行结果裁决。即使提示词被完全绕过，也无法产生一条"已验证但无执行记录"的交付项。'),
    ('h3', '（5）用户消息的上下文组装顺序'),
    ('code', '【代码切片】文件：<文件名>  符号：<函数名>  行：<起>-<止>\n'
             '【图谱上下文】单元位置；可达性；候选缺陷类型；消毒器\n'
             '【静态分析证据】\n'
             '  - 第 N 行 CWE-xx（类型名）：<代码片段>\n'
             '【历史经验】\n'
             '  - <标题>（历史判定：verified / refuted）\n'
             '【代码】\n'
             '```python\n<切片源码>\n```\n'
             '<输出结构约束>'),

    ('pagebreak',),
    ('h1', '附录D 原型运行记录'),
    ('h3', '（1）端到端演示输出（目标一：单文件）'),
    ('code', '目标：vulnapp（单文件目标：六类典型缺陷）\n'
             '  规模：1 个文件 / 92 行代码；函数与单元 7 个；外部入口 1 个\n'
             '  解析：调用点 31，内部 6，外部 6，无法静态确定 8，解析率 19%\n'
             '  沙箱：subprocess（隔离等级：进程级）\n'
             '  结果：切片 6 个；假设 6 条；确认 6 条\n'
             '--------------------------------------------------------------------------\n'
             '  [中危] CWE-327  confirmed_static userservice.py:66\n'
             '  [高危] CWE-78   verified         userservice.py:46\n'
             '  [高危] CWE-798  confirmed_static userservice.py:24\n'
             '  [高危] CWE-89   verified         userservice.py:40\n'
             '  [高危] CWE-22   verified         userservice.py:54\n'
             '  [严重] CWE-502  verified         userservice.py:61\n'
             '  攻击链 C-4d2dd5：凭据泄露叠加远程代码执行，形成完整的失陷链。'),
    ('h3', '（3）端到端演示输出（目标三：Web 服务片段）'),
    ('code', '目标：websvc（Web 服务片段：四类可动态验证的缺陷）\n'
             '  规模：1 个文件 / 59 行代码；函数与单元 5 个；外部入口 1 个\n'
             '  解析：调用点 16，内部 4，外部 2，无法静态确定 4，解析率 25%\n'
             '  结果：切片 4 个；假设 4 条；确认 4 条\n'
             '--------------------------------------------------------------------------\n'
             '  [中危] CWE-79    verified         portal.py:19\n'
             '  [中危] CWE-327   verified         portal.py:29\n'
             '  [高危] CWE-95    verified         portal.py:24\n'
             '  [高危] CWE-918   verified         portal.py:34'),
    ('h3', '（4）增量扫描演示（CI 场景）'),
    ('code', '增量扫描演示（先安全、后回归缺陷）\n'
             '  ① 首次扫描（建基线）   变更 3 文件 → 审计 0 个切片（跳过 0），确认 0 条\n'
             '  ② 重新引入缺陷后       变更 1 文件 → 审计 1 个切片（跳过 0），确认 1 条\n'
             '     [CWE-89] dao.py:24\n'
             '  ③ 无变更再跑一次       变更 0 文件 → 审计 0 个切片（跳过 1），确认 0 条'),
    ('h3', '（5）差分验证的执行证据（节选自 report.md）'),
    ('code', 'F-004 SQL 注入（find_user）\n'
             '- 验证结论：已差分验证（缺陷版复现效果、修复版未复现，差分成立）\n'
             '- 复现记录：\n'
             '  · 目标 vulnerable：退出码 0，耗时 78 毫秒，\n'
             '    观测 {"rows": 2, "data": [["alice","user"],["bob","admin"]]}\n'
             '  · 目标 patched：退出码 0，耗时 72 毫秒，观测 {"rows": 0, "data": []}\n'
             '\n'
             'CWE-79 反射型 XSS（render_comment）\n'
             '  · 目标 vulnerable：观测 {"raw_payload": true,\n'
             '    "output_head": "<div class=\'comment\'>Hello, <script>alert(1)</script>!</div>"}\n'
             '  · 目标 patched：观测 {"raw_payload": false,\n'
             '    "output_head": "...Hello, &lt;script&gt;alert(1)&lt;/script&gt;!..."}\n'
             '\n'
             'CWE-327 弱哈希（sign）\n'
             '  · 目标 vulnerable：观测 {"digest": "e10adc3949ba59abbe56e057f20f883e",\n'
             '    "recovered": true}\n'
             '  · 目标 patched：观测 {"digest": "b96e1e0c...", "recovered": false}\n'
             '\n'
             'CWE-918 服务端请求伪造（fetch）\n'
             '  · 目标 vulnerable：观测 {"leaked": true}\n'
             '  · 目标 patched：观测 {"leaked": false,\n'
             '    "error": "ValueError(\'仅允许 http/https 协议\')"}'),
    ('h3', '（6）自动化测试结果（31 项）'),
    ('code', 'python -m unittest discover -s tests -v\n'
             '\n'
             '# 规则引擎与推理器\n'
             'test_detects_all_planted_cwes ...................... ok\n'
             'test_patched_target_has_no_rule_hits .............. ok\n'
             'test_call_graph_and_reachability .................. ok\n'
             'test_propose_binds_evidence_and_preconditions ..... ok\n'
             'test_challenge_drops_sanitized_path ............... ok\n'
             'test_extract_json_tolerates_fences ................ ok\n'
             'test_extract_json_object .......................... ok\n'
             '# 调用图符号解析\n'
             'test_cross_file_reachability ...................... ok\n'
             'test_same_name_method_disambiguation .............. ok\n'
             'test_key_format_is_consistent ..................... ok\n'
             'test_unknown_calls_are_not_guessed ................ ok\n'
             '# 沙箱与差分验证\n'
             'test_differential_verifies_sql_injection .......... ok\n'
             'test_unknown_cwe_falls_back_to_static ............. ok\n'
             'test_sandbox_timeout_is_reported .................. ok\n'
             'test_poc_execution_error_is_not_treated_as_false_positive ok\n'
             '# 容器沙箱后端\n'
             'test_docker_command_carries_isolation_flags ....... ok\n'
             'test_backend_info_reports_effective_backend ....... ok\n'
             'test_unavailable_docker_degrades_explicitly ....... ok\n'
             'test_no_fallback_failure_never_becomes_false_negative ok\n'
             '# 增量扫描\n'
             'test_manifest_diff_detects_all_change_kinds ....... ok\n'
             'test_impact_analysis_limits_scope ................. ok\n'
             'test_no_change_skips_everything ................... ok\n'
             'test_changed_file_is_audited_and_others_skipped ... ok\n'
             '# 记忆库与端到端\n'
             'test_record_and_search ............................ ok\n'
             'test_full_scan_delivers_verified_findings ......... ok\n'
             'test_patched_target_yields_zero_findings .......... ok\n'
             'test_budget_stops_early ........................... ok\n'
             'test_webapp_sql_injection_verified_across_files ... ok\n'
             'test_four_new_types_all_dynamically_verified ...... ok\n'
             'test_patched_websvc_yields_zero_findings .......... ok\n'
             'test_weak_hash_verification_is_objective .......... ok\n'
             '\n'
             'Ran 31 tests in 8.9s\n'
             'OK'),
    ('h3', '（7）如何复现以上全部结果'),
    ('code', 'cd prototype\n'
             'python run_demo.py                                  # 三个目标 + 增量扫描演示\n'
             'python -m unittest discover -s tests -v             # 全部 31 项测试\n'
             'python -m vulnagent doctor                          # 环境自检\n'
             'python -m vulnagent scan --target demo/websvc \\\n'
             '       --patched demo/websvc_patched --out out       # 命令行扫描\n'
             'python -m vulnagent scan --target ./repo --incremental \\\n'
             '       --baseline out/vulnagent-baseline.json        # 增量扫描'),
    ('note', '说明：以上数据为本机实测结果，运行环境为 Windows + Python 3.12，沙箱后端为 subprocess，'
             '推理后端为 heuristic（离线确定性）。本机未安装容器运行时（仅有 WSL），'
             '因此容器后端的隔离参数通过单元测试验证，尚未在真实容器中执行；'
             '部署到具备容器运行时的环境后，将 backend 设为 docker、allow_fallback 设为 false 即可启用。'
             '切换为私有化模型后，假设生成的覆盖面会扩大，但验证与交付环节的判定规则不变——'
             '结论仍然只由可复现的执行证据决定。'),
]

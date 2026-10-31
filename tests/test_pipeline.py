import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

WORK=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(WORK/'common'))
sys.path.insert(0,str(WORK/'services/orchestrator'))
os.environ['OUTPUT_ROOT']=tempfile.mkdtemp()

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,WORK/path)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    return mod

common=load('helpers','common/common.py')
o=load('orchestrator_service','services/orchestrator/main.py')
t=load('tester_service','services/tester/main.py')
CONTRACT={'openapi':{'openapi':'3.0.3','paths':{'/api/items':{'get':{'responses':{'200':{'description':'ok'}}}}}},
          'acceptance_criteria':[{'id':'AC-1','text':'List items','roles':['BACKEND']}]}

class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_paths_and_archive(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError): common.contained(d,'../../etc/passwd')
            root=Path(d)/'src';root.mkdir();(root/'file.txt').write_text('hello')
            (root/'node_modules').mkdir();(root/'node_modules/secret').write_text('cache')
            archive=Path(d)/'out.zip';common.archive(root,archive)
            common.unpack(archive.read_bytes(),Path(d)/'restored')
            self.assertEqual((Path(d)/'restored/file.txt').read_text(),'hello')
            self.assertFalse((Path(d)/'restored/node_modules').exists())

    async def test_selected_components_and_final_artifact(self):
        for roles in (['BACKEND'],['WEB','BACKEND'],['MOBILE']):
            rid=str(uuid4());p=o.folder(rid);p.mkdir()
            o.save(p/'state.json',{'run_id':rid,'status':'queued'})
            plan={'tasks':[{'role':r,'task':'build','context':{}} for r in roles],'contract':CONTRACT}
            called=[]
            async def fake_request(url,data):
                if url.endswith('/analyze'): return {'success':True,'data':{'project_name':'probe'}}
                if url.endswith('/integrate'): return {'status':'passed'}
                role=next(r for r in roles if url.startswith('http://'+r.lower()+':'))
                called.append(role)
                source=p/role.lower();source.mkdir();(source/'README.md').write_text('component')
                archive=p/'approved'/(role.lower()+'.zip');archive.parent.mkdir(exist_ok=True)
                common.archive(source,archive)
                return {'role':role,'status':'passed','zip_path':str(archive),'contract_hash':o.digest(CONTRACT)}
            with patch.dict(os.environ,{r+'_URL':'http://'+r.lower()+':8000' for r in ['ANALYST','BACKEND','WEB','MOBILE','TESTER']}),\
                 patch.object(o,'request',side_effect=fake_request),\
                 patch.object(o,'plan',AsyncMock(return_value=(plan,10))),\
                 patch.object(o,'document',AsyncMock(return_value=({'README.md':'actual docs'},5))):
                await o.run(rid,'business request')
            self.assertEqual(o.get_run(rid)['status'],'completed')
            self.assertEqual(set(called),set(roles))
            self.assertTrue((p/'project.zip').exists())

    async def test_failed_component_prevents_final_zip(self):
        rid=str(uuid4());p=o.folder(rid);p.mkdir();o.save(p/'state.json',{'status':'queued'})
        plan={'tasks':[{'role':'BACKEND','task':'build','context':{}}],'contract':CONTRACT}
        with patch.dict(os.environ,{'BACKEND_URL':'http://backend:8000'}),\
             patch.object(o,'plan',AsyncMock(return_value=(plan,0))),\
             patch.object(o,'request',AsyncMock(return_value={'role':'BACKEND','status':'failed'})):
            await o.run(rid,'',{'project_name':'x'})
        self.assertEqual(o.get_run(rid)['status'],'error')
        self.assertFalse((p/'project.zip').exists())

    async def test_test_generation_retries_until_criteria_covered(self):
        contract={**CONTRACT,'acceptance_criteria':[{'id':'AC-1','text':'list','roles':['BACKEND']},
                                                    {'id':'AC-2','text':'list again','roles':['BACKEND']}]}
        step=[{'method':'GET','path':'/api/items','status':200}]
        partial=[{'name':'one','requirement_id':'AC-1','steps':step}]
        full=partial+[{'name':'two','requirement_id':'AC-2','steps':step}]
        with patch.object(t,'llm',AsyncMock(side_effect=[(partial,5),(full,6)])) as model:
            cases,tokens=await t.make_tests({'role':'BACKEND','requirements':{},'contract':contract},{})
        self.assertEqual(([c['requirement_id'] for c in cases],tokens),(['AC-1','AC-2'],11))
        self.assertIn('AC-2',model.await_args_list[1].args[1]['previous_attempt_problem'])
        with patch.object(t,'llm',AsyncMock(return_value=(partial,5))):  # never covered: run what exists
            cases,tokens=await t.make_tests({'role':'BACKEND','requirements':{},'contract':contract},{})
        self.assertEqual((len(cases),tokens),(1,15))

    async def test_startup_rejection_skips_test_generation(self):
        rid=str(uuid4());source=t.ROOT/rid/'web';source.mkdir(parents=True)
        data={'run_id':rid,'role':'WEB','source':str(source),'attempt':1,'contract':CONTRACT,'requirements':{}}
        with patch.object(t,'command',AsyncMock(return_value={'ok':False,'output':'build failed'})),\
             patch.object(t,'make_tests',AsyncMock()) as generate:
            result=await t.validate(data)
        self.assertEqual(result['status'],'rejected')
        self.assertEqual(result['stage'],'build')
        generate.assert_not_called()
        self.assertNotIn('zip_path',result)

    async def test_contract_hash_mismatch_prevents_assembly(self):
        rid=str(uuid4());p=o.folder(rid);p.mkdir();o.save(p/'state.json',{'status':'queued'})
        planned={'tasks':[{'role':'WEB','task':'build','context':{}}],'contract':CONTRACT}
        with patch.dict(os.environ,{'WEB_URL':'http://web:8000'}),\
             patch.object(o,'plan',AsyncMock(return_value=(planned,0))),\
             patch.object(o,'request',AsyncMock(return_value={'role':'WEB','status':'passed','contract_hash':'wrong'})):
            await o.run(rid,'',{'project_name':'x'})
        self.assertEqual(o.get_run(rid)['status'],'error')

    async def test_http_steps_send_body_and_chain_ids(self):
        import httpx
        seen=[]
        def handler(req):
            seen.append((req.method,req.url.path,req.content))
            if req.method=='POST': return httpx.Response(201,json={'id':'n1','title':'T'})
            if req.method=='GET': return httpx.Response(200,json=[{'id':'n0','title':'X'},{'id':'n1','title':'T'}])
            return httpx.Response(204)
        real=httpx.AsyncClient
        case={'name':'create+delete','requirement_id':'AC-1','steps':[
            {'method':'POST','path':'/api/items','body':{'title':'T','text':{'$repeat':'a','times':3}},'status':201,'json_contains':{'title':'T'}},
            {'method':'DELETE','path':'/api/items/{id}','status':204}]}
        listing={'name':'list','requirement_id':'AC-1','steps':[
            {'method':'GET','path':'/api/items','status':200,'json_contains':{'title':'T'}}]}
        with patch.object(t.httpx,'AsyncClient',lambda **kw:real(transport=httpx.MockTransport(handler),**kw)):
            result=await t.http_tests('http://app',[case,listing])
        self.assertTrue(all(r['passed'] for r in result),result)
        bad={'name':'x','requirement_id':'AC-1','steps':[{'method':'GET','path':'/api/items','status':200,'json_contains':{'message':''}}]}
        with self.assertRaises(ValueError): t.validate_cases([bad],'BACKEND',CONTRACT)
        self.assertEqual(json.loads(seen[0][2]),{'title':'T','text':'aaa'})
        self.assertEqual(seen[1][:2],('DELETE','/api/items/n1'))

    def test_attempt_reports_skip_test_case_files(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ('attempt-10.json','attempt-2.json','attempt-2.tests.json'):
                (Path(d)/name).write_text('{}')
            self.assertEqual([p.name for p in common.attempt_reports(d)],['attempt-2.json','attempt-10.json'])

    def test_placeholders_in_body_and_step_references(self):
        saved={'id':'book-7','step1.id':'author-1','step1.n':3}
        self.assertEqual(t.substitute({'authorId':'{step1.id}','copies':'{step1.n}','note':'by {step1.id}'},saved),
                         {'authorId':'author-1','copies':3,'note':'by author-1'})
        self.assertEqual(t.substitute('/api/authors/{step1.id}?x={id}',saved),'/api/authors/author-1?x=book-7')
        self.assertEqual(t.substitute('{unknown}',saved),'{unknown}')

    def test_web_actions_are_validated(self):
        ok={'name':'convert','requirement_id':'AC-1','path':'/','visible_text':'1.609',
            'actions':[{'fill':'Значение','value':'1'},{'select':'Из','option':'Мили'},{'click':'Конвертировать'}]}
        t.validate_cases([ok],'WEB',CONTRACT)
        for bad in ([{'drag':'x'}],[{'fill':'Значение'}],[{'select':'Из'}],[{'click':''}],[{'click':'a','fill':'b','value':'c'}]):
            with self.assertRaises(ValueError): t.validate_cases([{**ok,'actions':bad}],'WEB',CONTRACT)

    def test_openapi_nullable_accepts_null(self):
        import jsonschema
        schema=t.nullable({'type':'object','properties':{'text':{'type':'string','nullable':True}}})
        jsonschema.validate({'text':None},schema)
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate({'text':1},schema)

    def test_generated_tests_cannot_target_external_host(self):
        with self.assertRaises(ValueError):
            t.validate_cases([{'name':'bad','requirement_id':'AC-1','steps':[{'method':'GET','path':'//external','status':200}]}],'BACKEND',CONTRACT)
        with self.assertRaises(ValueError): t.validate_cases([],'WEB',CONTRACT)

class AgentCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_retries_tester_and_forwards_only_approved_path(self):
        with patch.dict(os.environ,{'AGENT_ROLE':'WEB','TESTER_URL':'http://tester:8000','MAX_REPAIR_ATTEMPTS':'3'}):
            agent=load('agent_service','services/web/main.py')
            with tempfile.TemporaryDirectory(dir=WORK) as d:
                agent.ROOT=Path(d)
                rid=str(uuid4())
                replies=[{'status':'rejected','error':'not started','feedback':{'errors':[{'message':'startup failed'}]}},
                         {'status':'passed','zip_path':str(Path(d)/rid/'approved/web.zip')}]
                with patch.object(agent,'generate',AsyncMock(return_value={'status':'ok','tokens':5})) as generate, \
                     patch.object(agent,'request',AsyncMock(side_effect=replies)) as tester:
                    result=await agent.work({'run_id':rid,'task':'build web','context':{},'contract':CONTRACT})
                self.assertEqual(result['status'],'passed')
                self.assertEqual(result['attempts'],2)
                self.assertEqual(generate.await_count,2)
                self.assertEqual(tester.await_count,2)
                self.assertTrue(result['zip_path'].endswith('approved/web.zip'))

class ReportTests(unittest.IsolatedAsyncioTestCase):
    def test_failure_summary_names_stage_and_cause(self):
        results=[{'role':'WEB','status':'passed','contract_hash':'h'},
                 {'role':'BACKEND','status':'failed','attempts':3,'reports':[{'attempt':3,'test':{'status':'rejected','stage':'build',
                  'error':'...long log...\nerror NU1301: Unable to load the service index for source https://api.nuget.org'}}]}]
        text=o.failure_summary(results,'h')
        self.assertIn('BACKEND: попыток 3, стадия build',text)
        self.assertIn('NU1301',text)
        self.assertNotIn('WEB',text)

    async def test_report_written_for_failed_run(self):
        rid=str(uuid4());p=o.folder(rid);p.mkdir()
        o.save(p/'state.json',{'run_id':rid,'status':'queued'});o.save(p/'input.json',{'text':'Сервис «Мои заметки»'})
        async def reject(url,data): return {'success':False,'error':'schema mismatch'}
        with patch.dict(os.environ,{'ANALYST_URL':'http://analyst:8000'}),patch.object(o,'request',side_effect=reject):
            await o.run(rid,'Сервис «Мои заметки»')
        text=(p/'REPORT.md').read_text(encoding='utf-8')
        self.assertIn('**Статус:** error',text)
        self.assertIn('> Сервис «Мои заметки»',text)
        self.assertIn('ТЗ не сформировано',text)
        self.assertEqual(o.run_report(rid).body.decode(),text)

    async def test_report_for_completed_run_has_tests_and_coverage(self):
        rid=str(uuid4());p=o.folder(rid);p.mkdir();o.save(p/'state.json',{'run_id':rid,'status':'queued'})
        plan={'tasks':[{'role':'BACKEND','task':'build','context':{}}],'contract':CONTRACT}
        async def fake_request(url,data):
            if url.endswith('/integrate'): return {'status':'passed','cases':[{'name':'list','passed':True}]}
            src=p/'backend';src.mkdir(exist_ok=True);(src/'Program.cs').write_text('a\nb\n')
            o.save(p/'qa/backend/attempt-1.json',{'status':'passed','cases':[{'name':'list items','requirement_id':'AC-1','passed':True}]})
            archive=p/'approved/backend.zip';archive.parent.mkdir(exist_ok=True);common.archive(src,archive)
            return {'role':'BACKEND','status':'passed','zip_path':str(archive),'contract_hash':o.digest(CONTRACT),'attempts':1,'tokens':42}
        with patch.dict(os.environ,{'BACKEND_URL':'http://backend:8000','TESTER_URL':'http://tester:8000'}),\
             patch.object(o,'request',side_effect=fake_request),patch.object(o,'plan',AsyncMock(return_value=(plan,10))),\
             patch.object(o,'document',AsyncMock(return_value=({'README.md':'docs'},5))):
            await o.run(rid,'',{'project_name':'probe'})
        self.assertEqual(o.get_run(rid)['status'],'completed')
        text=(p/'REPORT.md').read_text(encoding='utf-8')
        for part in ('**Статус:** completed','`GET /api/items`','`Program.cs` — 2 строк','| list items | AC-1 | пройден |',
                     '| AC-1 | List items | BACKEND | passed | 1 |','Токены всего: 57'):
            self.assertIn(part,text)
        import zipfile
        self.assertIn('REPORT.md',zipfile.ZipFile(p/'project.zip').namelist())

class DocsFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_docs_failure_keeps_tested_product(self):
        rid=str(uuid4());p=o.folder(rid);p.mkdir();o.save(p/'state.json',{'run_id':rid,'status':'queued'})
        plan={'tasks':[{'role':'BACKEND','task':'build','context':{}}],'contract':CONTRACT}
        async def fake_request(url,data):
            if url.endswith('/integrate'): return {'status':'passed','cases':[]}
            src=p/'backend';src.mkdir(exist_ok=True);(src/'Program.cs').write_text('x')
            archive=p/'approved/backend.zip';archive.parent.mkdir(exist_ok=True);common.archive(src,archive)
            return {'role':'BACKEND','status':'passed','zip_path':str(archive),'contract_hash':o.digest(CONTRACT),'tokens':1}
        with patch.dict(os.environ,{'BACKEND_URL':'http://backend:8000','TESTER_URL':'http://tester:8000'}),\
             patch.object(o,'request',side_effect=fake_request),patch.object(o,'plan',AsyncMock(return_value=(plan,1))),\
             patch.object(o,'document',AsyncMock(side_effect=ValueError('DOCS: LLM unavailable: 429'))):
            await o.run(rid,'',{'project_name':'probe'})
        self.assertEqual(o.get_run(rid)['status'],'completed')
        self.assertIn('# Отчёт о прогоне',(p/'product/docs/README.md').read_text(encoding='utf-8'))
        self.assertIn('Документацию модель не написала',(p/'REPORT.md').read_text(encoding='utf-8'))

class DisabledRolesTests(unittest.IsolatedAsyncioTestCase):
    def test_planner_drops_disabled_tasks_but_keeps_contract(self):
        planner=load('planner_disabled','services/planner/main.py')
        plan={'tasks':[{'role':'BACKEND','task':'b','context':{}},{'role':'MOBILE','task':'m','context':{}}],'contract':CONTRACT}
        result=planner.drop_disabled(plan,{'MOBILE'})
        self.assertEqual(([t['role'] for t in result['tasks']],result['disabled'],result['contract']),(['BACKEND'],['MOBILE'],CONTRACT))
        with self.assertRaises(ValueError) as e:
            planner.drop_disabled({'tasks':[{'role':'MOBILE','task':'m','context':{}}],'contract':CONTRACT},{'MOBILE'})
        self.assertIn('MOBILE',str(e.exception))

    async def test_disabled_component_criteria_and_report(self):
        rid=str(uuid4());p=o.folder(rid);p.mkdir();o.save(p/'state.json',{'run_id':rid,'status':'queued'})
        contract={**CONTRACT,'acceptance_criteria':[{'id':'AC-1','text':'List items','roles':['BACKEND']},
                                                    {'id':'AC-2','text':'Push «10 минут»','roles':['MOBILE']}]}
        plan={'tasks':[{'role':'BACKEND','task':'build','context':{}}],'contract':contract,'disabled':['MOBILE']}
        async def fake_request(url,data):
            if url.endswith('/integrate'): return {'status':'passed','cases':[]}
            src=p/'backend';src.mkdir(exist_ok=True);(src/'Program.cs').write_text('x')
            o.save(p/'qa/backend/attempt-1.json',{'status':'passed','cases':[{'name':'list','requirement_id':'AC-1','passed':True}]})
            archive=p/'approved/backend.zip';archive.parent.mkdir(exist_ok=True);common.archive(src,archive)
            return {'role':'BACKEND','status':'passed','zip_path':str(archive),'contract_hash':o.digest(contract),'tokens':1}
        with patch.dict(os.environ,{'BACKEND_URL':'http://backend:8000','TESTER_URL':'http://tester:8000'}),\
             patch.object(o,'request',side_effect=fake_request),patch.object(o,'plan',AsyncMock(return_value=(plan,1))),\
             patch.object(o,'document',AsyncMock(return_value=({'README.md':'d'},1))):
            await o.run(rid,'',{'project_name':'probe'})
        self.assertEqual((o.get_run(rid)['status'],o.get_run(rid)['disabled']),('completed',['MOBILE']))
        coverage=json.loads((p/'product/coverage.json').read_text())
        self.assertEqual([c['status'] for c in coverage],['passed','disabled'])
        text=(p/'REPORT.md').read_text(encoding='utf-8')
        self.assertIn('не генерируется: компонент отключён',text)
        self.assertIn('| AC-2 | Push «10 минут» | MOBILE | компонент отключён | 0 |',text)
        self.assertNotIn('Не подтверждены тестами',text)
        self.assertIn('Не сгенерированы отключённые компоненты: Mobile',text)
        events=(p/'events.jsonl').read_text(encoding='utf-8')
        self.assertIn('Не генерируются (компонент отключён): MOBILE',events)

class PlannerDocsServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_rejection_reaches_run_state(self):
        import httpx
        rid=str(uuid4());p=o.folder(rid);p.mkdir();o.save(p/'state.json',{'status':'queued'})
        rejected=httpx.Response(422,json={'detail':'Invalid component selection'},request=httpx.Request('POST','http://planner:8000/plan'))
        async def fail(url,data): raise httpx.HTTPStatusError('422',request=rejected.request,response=rejected)
        with patch.dict(os.environ,{'PLANNER_URL':'http://planner:8000'}),patch.object(o,'request',side_effect=fail):
            await o.run(rid,'',{'project_name':'x'})
        self.assertEqual(o.get_run(rid)['error'],'PLANNER: Invalid component selection')

    async def test_planner_service_validates_plan(self):
        import httpx
        planner=load('planner_service','services/planner/main.py')
        bad={'tasks':[{'role':'DESKTOP','task':'x','context':{}}],'contract':CONTRACT}
        transport=httpx.ASGITransport(app=planner.app)
        async with httpx.AsyncClient(transport=transport,base_url='http://planner') as c:
            with patch.object(planner,'llm',AsyncMock(return_value=(bad,3))):
                r=await c.post('/plan',json={'spec':{}})
            self.assertEqual((r.status_code,r.json()['detail']),(422,'Invalid component selection'))
            good={'tasks':[{'role':'BACKEND','task':'build','context':{}}],'contract':CONTRACT}
            with patch.object(planner,'llm',AsyncMock(return_value=(good,3))):
                r=await c.post('/plan',json={'spec':{}})
            self.assertEqual(r.json(),{'plan':good,'tokens':3})

    async def test_docs_service_rejects_non_file_map(self):
        import httpx
        docs=load('docs_service','services/docs/main.py')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=docs.app),base_url='http://docs') as c:
            with patch.object(docs,'llm',AsyncMock(return_value=(['not','a','map'],1))):
                self.assertEqual((await c.post('/document',json={})).status_code,422)
            with patch.object(docs,'llm',AsyncMock(return_value=({'README.md':'# x'},4))):
                self.assertEqual((await c.post('/document',json={})).json(),{'docs':{'README.md':'# x'},'tokens':4})

class AgentOwnBuildTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_own_build_skips_tester_and_feeds_errors_back(self):
        with patch.dict(os.environ,{'AGENT_ROLE':'BACKEND','TESTER_URL':'http://tester:8000','MAX_REPAIR_ATTEMPTS':'3'}):
            agent=load('agent_backend','services/backend/main.py')
            with tempfile.TemporaryDirectory(dir=WORK) as d:
                agent.ROOT=Path(d);rid=str(uuid4())
                partial={'status':'partial','tokens':7,'steps':{'build_errors':{'Models/Task.cs':["CS0117: 'TaskStatus' does not contain 'Новая'"]}}}
                ok={'status':'ok','tokens':3}
                passed={'status':'passed','zip_path':str(Path(d)/rid/'approved/backend.zip')}
                with patch.object(agent,'generate',AsyncMock(side_effect=[partial,ok])) as generate,\
                     patch.object(agent,'request',AsyncMock(return_value=passed)) as tester:
                    result=await agent.work({'run_id':rid,'task':'build','context':{},'contract':CONTRACT})
                self.assertEqual((result['status'],result['attempts'],result['tokens']),('passed',2,10))
                self.assertEqual(tester.await_count,1)  # only the attempt that built
                self.assertIn('CS0117',generate.await_args_list[1].args[0])  # errors reach the next generation

class LlmOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_llm_stops_without_retries(self):
        with patch.dict(os.environ,{'AGENT_ROLE':'WEB','TESTER_URL':'http://tester:8000','MAX_REPAIR_ATTEMPTS':'3'}):
            agent=load('agent_web_outage','services/web/main.py')
            with tempfile.TemporaryDirectory(dir=WORK) as d:
                agent.ROOT=Path(d)
                down={'status':'failed','steps':{'design':{'status':'failed','error':'модель недоступна: All connection attempts failed'}}}
                with patch.object(agent,'generate',AsyncMock(return_value=down)) as generate,\
                     patch.object(agent,'request',AsyncMock()) as tester:
                    result=await agent.work({'run_id':str(uuid4()),'task':'build','context':{},'contract':CONTRACT})
        self.assertEqual((result['status'],result['stage'],result['attempts']),('failed','llm',1))
        self.assertEqual((generate.await_count,tester.await_count),(1,0))
        self.assertEqual(o.failure_summary([result],'h'),'WEB: Модель (LLM) недоступна — проверьте туннель/LLM_URL')

class CoderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_developer_round_trip(self):
        import types
        import httpx
        sys.path.insert(0,str(WORK/'common'))
        import coder_client,coder_service
        usage={}
        def set_debug_dir(p): remote_llm.debug=p
        remote_llm=types.SimpleNamespace(USAGE={},set_debug_dir=set_debug_dir,debug=None)
        async def write_file(design,spec,written,retries=1):
            remote_llm.USAGE.setdefault('developer',{'calls':0,'tokens':0})
            remote_llm.USAGE['developer']['calls']+=1;remote_llm.USAGE['developer']['tokens']+=10
            return {'code':f"{remote_dev.JSON_NAMING}|{spec['path']}|{remote_llm.debug}",'problems':[],'attempts':1}
        remote_dev=types.SimpleNamespace(JSON_NAMING='',write_file=write_file)
        app=coder_service.build_app('BACKEND',remote_dev,remote_llm)
        local_dev=types.SimpleNamespace(JSON_NAMING='',check_file=lambda p,c:['local'])
        local_llm=types.SimpleNamespace(USAGE=usage,_DEBUG_DIR=Path('/tmp/dbg'))
        dev=coder_client.RemoteDeveloper('http://coder:8000',local_dev,local_llm)
        dev.JSON_NAMING='snake'
        real=httpx.AsyncClient
        with patch.object(coder_client.httpx,'AsyncClient',lambda **kw:real(transport=httpx.ASGITransport(app=app),**kw)):
            r1=await dev.write_file({},{'path':'A.cs'},{})
            r2=await dev.write_file({},{'path':'B.cs'},{})
            with self.assertRaises(RuntimeError): await dev._call('drop_db',[],{})
        self.assertEqual(r1['code'].split('|')[:2],['snake','A.cs'])
        self.assertTrue(r1['code'].endswith('dbg'))
        self.assertIs(local_llm.USAGE,usage)  # teamlead keeps a reference to this dict
        self.assertEqual(usage['developer'],{'calls':2,'tokens':20})
        self.assertEqual(dev.check_file('x','y'),['local'])
        self.assertEqual(local_dev.JSON_NAMING,'snake')

if __name__=='__main__': unittest.main()

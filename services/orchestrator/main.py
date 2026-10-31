import asyncio
import io
import json
import os
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4, UUID

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
import httpx
from common import ROOT, attempt_reports, contained, digest, request, save, unpack
import report

jobs=set()
serial=asyncio.Lock()

@asynccontextmanager
async def lifespan(app):
    for p in ROOT.glob('*/state.json'):
        data=json.loads(p.read_text())
        if data['status'] in ('queued','running'):
            data.update(status='interrupted',message='Orchestrator restarted. Start a new run.')
            save(p,data)
    yield
    for job in jobs: job.cancel()
    await asyncio.gather(*jobs,return_exceptions=True)

app=FastAPI(title='MAS product pipeline',lifespan=lifespan)


def folder(rid):
    return ROOT/str(UUID(rid))

def emit(rid,agent,message,**extra):
    path=folder(rid)
    event={'agent':agent,'message':message,'timestamp':time.time(),**extra}
    with (path/'events.jsonl').open('a') as f:
        f.write(json.dumps(event,ensure_ascii=False)+'\n')

def state(rid,**update):
    p=folder(rid)/'state.json';data=json.loads(p.read_text());data.update(update);save(p,data)

async def service(name,path,payload):
    # planner/docs rejection (422) or LLM outage (502): a pipeline error with its reason, not an HTTP trace
    try:
        return await request(os.environ[name+'_URL']+path,payload)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (422,502):
            raise ValueError(f"{name}: {e.response.json().get('detail')}") from e
        raise

# components switched off until their agent is ready, e.g. DISABLED_ROLES=MOBILE
DISABLED_ROLES=[r.strip().upper() for r in os.getenv('DISABLED_ROLES','').split(',') if r.strip()]

async def plan(spec):
    answer=await service('PLANNER','/plan',{'spec':spec,'disabled_roles':DISABLED_ROLES})
    return answer['plan'],answer['tokens']

async def document(payload):
    answer=await service('DOCS','/document',payload)
    return answer['docs'],answer['tokens']

def failure_summary(results,hash_):
    """Why each component was not approved: last tester verdict, not the whole report."""
    lines=[]
    for r in results:
        if r.get('status')=='passed' and r.get('contract_hash')==hash_:
            continue
        reports=r.get('reports') or []
        test=reports[-1]['test'] if reports else {}
        stage=test.get('stage') or ('tests' if test.get('cases') else '')
        if r.get('stage')=='llm':  # infrastructure failure, earlier attempts are irrelevant
            lines.append(f"{r.get('role')}: {r.get('error')}");continue
        if test.get('cases'):
            failed=[c for c in test['cases'] if not c.get('passed')]
            reason='; '.join(f"{c.get('name')}: {str(c.get('error',''))[:200]}" for c in failed[:3])
            reason=f'не прошли тесты {len(failed)} из {len(test["cases"])}: {reason}'
        else:
            reason=str(test.get('error') or r.get('error') or 'нет отчёта')
            reason=reason.strip()[-600:]  # build/startup logs: the end holds the actual error
        lines.append(f"{r.get('role')}: попыток {r.get('attempts') or len(reports)}"
                     +(f', стадия {stage}' if stage else '')+f' — {reason}')
    return ' | '.join(lines)

async def run(rid,text,spec=None):
    started=time.monotonic();path=folder(rid)
    try:
        async with serial:
            state(rid,status='running')
            if spec is None:
                emit(rid,'ANALYST','Анализ бизнес-требований')
                answer=await request(os.environ['ANALYST_URL']+'/analyze',{'text':text})
                save(path/'analysis.json',answer)
                if not answer.get('success') or not answer.get('data'):
                    raise ValueError('Analyst rejected requirements: '+str(answer.get('error')))
                spec=answer['data']
            save(path/'specification.json',spec)
            emit(rid,'ORCHESTRATOR','Планирование компонентов и фиксация контрактов')
            planned,tokens=await plan(spec)
            save(path/'plan.json',planned)
            contract=planned['contract'];hash_=digest(contract)
            save(path/'contracts/contract.json',contract)
            save(path/'contracts/openapi.json',contract['openapi'])
            expected=[t['role'] for t in planned['tasks']]
            state(rid,expected=expected,contract_hash=hash_)
            emit(rid,'ORCHESTRATOR','Выбраны компоненты: '+', '.join(expected),tokens=tokens)
            if planned.get('disabled'):
                state(rid,disabled=planned['disabled'])
                emit(rid,'ORCHESTRATOR','Не генерируются (компонент отключён): '+', '.join(planned['disabled']))
            async def component(task):
                role=task['role']
                emit(rid,role,'Генерация и прямой цикл с tester')
                payload={'run_id':rid,'task':task['task'],'context':{
                    **task['context'],'acceptance_criteria':[a for a in contract['acceptance_criteria'] if role in a.get('roles',[])]},
                    'contract':contract}
                result=await request(os.environ[role+'_URL']+'/work',payload)
                save(path/(role.lower()+'-result.json'),result)
                emit(rid,role,'Цикл завершён: '+result['status'],tokens=result.get('tokens',0))
                return result
            raw=await asyncio.gather(*(component(t) for t in planned['tasks']),return_exceptions=True)
            results=[]
            for item in raw:
                if isinstance(item,BaseException):
                    raise RuntimeError(str(item))
                results.append(item)
            save(path/'components.json',results)
            approved={r['role']:r for r in results if r.get('status')=='passed' and r.get('contract_hash')==hash_}
            if set(approved)!=set(expected):
                raise ValueError('Компоненты не приняты тестировщиком. '+failure_summary(results,hash_))
            product=path/'product';product.mkdir()
            for role,r in approved.items():
                artifact=contained(path,r['zip_path'])
                unpack(artifact.read_bytes(),product/role.lower())
            shutil_contract=product/'contracts';shutil_contract.mkdir()
            save(shutil_contract/'contract.json',contract)
            save(shutil_contract/'openapi.json',contract['openapi'])
            write_delivery(product,expected)
            emit(rid,'ORCHESTRATOR','Все выбранные ZIP получены. Проверка стыковки компонентов')
            integration=await request(os.environ['TESTER_URL']+'/integrate',{
                'run_id':rid,'roles':expected,'contract':contract})
            save(path/'integration.json',integration)
            if integration['status']!='passed':
                raise ValueError('Integration rejected: '+json.dumps(integration,ensure_ascii=False)[:3000])
            coverage=[]
            for criterion in contract['acceptance_criteria']:
                evidence=[]
                for rp in attempt_reports(path/'qa','*/attempt-*.json'):
                    rr=json.loads(rp.read_text())
                    evidence += [{'report':str(rp.relative_to(path)),'test':x['name'],'passed':x['passed']}
                                 for x in rr.get('cases',[]) if x.get('requirement_id')==criterion['id']]
                roles=set(criterion.get('roles',[]))
                if roles and roles<=set(planned.get('disabled',[])):
                    coverage.append({**criterion,'status':'disabled','evidence':[]});continue
                coverage.append({**criterion,'status':'passed' if evidence and all(e['passed'] for e in evidence) else 'unverified',
                                 'evidence':evidence})
            save(product/'coverage.json',coverage)
            emit(rid,'DOCUMENTATION','Документация по фактическому результату')
            inventory=[str(p.relative_to(product)) for p in product.rglob('*') if p.is_file()][:300]
            try:
                docs,doc_tokens=await document(
                   {'spec':spec,'contract':contract,'files':inventory,'integration':integration,'coverage':coverage})
                emit(rid,'DOCUMENTATION','Документация готова',tokens=doc_tokens)
            except Exception as e:
                # the product is built and tested: missing prose must not throw it away
                docs={'README.md':report.build(path,{'status':'completed'})};doc_tokens=0
                save(path/'docs-error.json',{'error':str(e)})
                emit(rid,'DOCUMENTATION','Модель не написала документацию ('+str(e)[:200]+'); в docs/ — отчёт о прогоне')
            for name,content in docs.items():
                target=contained(product/'docs',name)
                target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content)
            save(product/'manifest.json',{'run_id':rid,'roles':expected,'contract_hash':hash_,
                 'tokens':tokens+doc_tokens+sum(r.get('tokens',0) for r in results),
                 'seconds':round(time.monotonic()-started,2),'components':results,'integration':integration,
                 'analyst_token_usage':'not supplied by original analyst service'})
            seconds=round(time.monotonic()-started,2)
            report.write(path,{'status':'completed','seconds':seconds})
            from common import archive
            archive(product,path/'project.zip')
            state(rid,status='completed',seconds=seconds)
            emit(rid,'ORCHESTRATOR',f'Отчёт о прогоне: /runs/{rid}/report')
            emit(rid,'ORCHESTRATOR','Продукт собран',status='completed',type='file_generated')
    except asyncio.CancelledError:
        state(rid,status='interrupted');raise
    except Exception as e:
        state(rid,status='error',error=str(e),seconds=round(time.monotonic()-started,2))
        report.write(path)
        emit(rid,'ORCHESTRATOR',f'Отчёт о прогоне: /runs/{rid}/report')
        emit(rid,'ORCHESTRATOR',str(e),status='error')

def write_delivery(product,roles):
    import yaml
    services={}
    if 'BACKEND' in roles:
        services['db']={'image':'postgis/postgis:17-3.5','environment':{
            'POSTGRES_DB':'app','POSTGRES_USER':'app','POSTGRES_PASSWORD':'${DB_PASSWORD:?Set DB_PASSWORD}'},
            'healthcheck':{'test':['CMD-SHELL','pg_isready -U app'],'interval':'5s','retries':30}}
        services['backend']={'build':'./backend','ports':['${BACKEND_PORT:-8080}:8080'],
            'environment':{'ConnectionStrings__Default':'Host=db;Database=app;Username=app;Password=${DB_PASSWORD}'},
            'depends_on':{'db':{'condition':'service_healthy'}}}
    if 'WEB' in roles:
        services['web']={'build':'./web','ports':['${WEB_PORT:-4200}:80']}
    if services:
        (product/'docker-compose.yml').write_text(yaml.safe_dump({'services':services},sort_keys=False))
    (product/'.env.example').write_text('DB_PASSWORD=change-me\nBACKEND_PORT=8080\nWEB_PORT=4200\n')
    (product/'README.md').write_text('# Generated product\n\nCopy .env.example to .env, set DB_PASSWORD.\n'
        'Run `docker compose up --build -d` for server/web components.\n'
        'Android build and launch instructions are in mobile/README.md.\n'
        'See docs/, coverage.json and manifest.json for evidence and limits.\n')
    ci={'stages':['build']}
    for role in roles:
        if role=='BACKEND': ci['backend']={'stage':'build','image':'mcr.microsoft.com/dotnet/sdk:10.0','script':['cd backend','dotnet build']}
        if role=='WEB': ci['web']={'stage':'build','image':'node:22','script':['cd web','npm install','npm run build']}
        if role=='MOBILE': ci['mobile']={'stage':'build','image':'mas-android-runner','script':['cd mobile','test -x ./gradlew','./gradlew assembleDebug testDebugUnitTest']}
    (product/'.gitlab-ci.yml').write_text(yaml.safe_dump(ci,sort_keys=False))

def launch(text,spec=None):
    rid=str(uuid4());folder(rid).mkdir()
    save(folder(rid)/'state.json',{'run_id':rid,'status':'queued','created_at':time.time()})
    save(folder(rid)/'input.json',{'text':text})
    job=asyncio.create_task(run(rid,text,spec));jobs.add(job);job.add_done_callback(jobs.discard)
    return rid

@app.get('/health')
def health(): return {'status':'ok'}

@app.post('/runs',status_code=202)
async def create(data:dict):
    text=data.get('text','')
    if not isinstance(text,str) or len(text.strip())<10:
        raise HTTPException(400,'text must contain business requirements')
    return {'run_id':launch(text)}

@app.post('/orchestrate',status_code=202)
async def orchestrate(spec:dict): return {'run_id':launch('',spec)}

@app.get('/runs')
def list_runs():
    return sorted([json.loads(p.read_text()) for p in ROOT.glob('*/state.json')],key=lambda r:r['created_at'],reverse=True)

@app.get('/runs/{rid}')
def get_run(rid:str):
    p=folder(rid)/'state.json'
    if not p.exists(): raise HTTPException(404,'Unknown run')
    return json.loads(p.read_text())

@app.get('/runs/{rid}/download')
def download(rid:str):
    p=folder(rid)/'project.zip'
    if not p.exists(): raise HTTPException(409,'No approved final archive')
    return FileResponse(p,filename='project.zip')

@app.get('/runs/{rid}/report')
def run_report(rid:str):
    from fastapi.responses import PlainTextResponse
    p=folder(rid)/'REPORT.md'
    if not p.exists(): raise HTTPException(409,'Report is written when the run finishes')
    return PlainTextResponse(p.read_text(encoding='utf-8'),media_type='text/markdown; charset=utf-8')

@app.get('/api/files')
def files(run_id:str):
    base=folder(run_id)
    def tree(p):
        return [{'name':x.name,'path':str(x.relative_to(base)),'type':'folder' if x.is_dir() else 'file',
                 **({'children':tree(x)} if x.is_dir() else {})}
                for x in sorted(p.iterdir()) if x.name not in ('node_modules','bin','obj','.gradle','build','.angular','dist','debug')]
    return tree(base)

@app.get('/api/files/content')
def content(run_id:str,path:str):
    from fastapi.responses import PlainTextResponse
    p=contained(folder(run_id),path)
    if not p.is_file() or p.stat().st_size>2_000_000: raise HTTPException(404,'File unavailable')
    try: return PlainTextResponse(p.read_text())
    except UnicodeError: raise HTTPException(415,'Binary file; download archive')

@app.websocket('/ws/pipeline')
async def websocket(ws:WebSocket):
    await ws.accept()
    try:
        data=await ws.receive_json()
        rid=data.get('run_id')
        if rid: get_run(rid)
        else:
            text=data.get('task','')
            if len(text.strip())<10: raise ValueError('Business requirements required')
            rid=launch(text)
        await ws.send_json({'run_id':rid,'agent':'SYSTEM','message':'Прогон '+rid})
        index=0
        while True:
            p=folder(rid)/'events.jsonl'
            lines=p.read_text().splitlines() if p.exists() else []
            for line in lines[index:]:
                event=json.loads(line)
                await ws.send_json({**event,'run_id':rid})
                if event.get('status') in ('completed','error'):
                    return
            index=len(lines)
            status=get_run(rid)['status']
            if status in ('completed','error','interrupted'):
                await ws.send_json({'run_id':rid,'status':'completed' if status=='completed' else 'error',
                                    'message':status});break
            await asyncio.sleep(1)
    except WebSocketDisconnect: pass  # generation continues independently
    except Exception as e:
        await ws.send_json({'status':'error','message':str(e)})

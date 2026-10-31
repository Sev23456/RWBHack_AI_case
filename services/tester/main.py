import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import jsonschema
from fastapi import FastAPI
from common import ROOT, archive, attempt_reports, contained, digest, llm, save

app=FastAPI(title='Independent application tester')
NETWORK=os.getenv('RUNNER_NETWORK','mas-runners')

async def command(args, timeout=1200):
    proc=await asyncio.create_subprocess_exec(*args,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    try:
        output,_=await asyncio.wait_for(proc.communicate(),timeout)
    except asyncio.TimeoutError:
        proc.kill();await proc.communicate()
        return {'ok':False,'output':'Command timed out','command':args}
    return {'ok':proc.returncode==0,'exit_code':proc.returncode,
            'output':output.decode(errors='replace')[-30000:],'command':args}

async def wait_http(url, seconds=90):
    deadline=time.monotonic()+seconds
    async with httpx.AsyncClient(timeout=3) as client:
        while time.monotonic()<deadline:
            try:
                r=await client.get(url)
                if r.status_code==200:
                    return True
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
    return False

def validate_cases(cases, role, contract):
    if not isinstance(cases,list) or not cases or len(cases)>30:
        raise ValueError('Expected 1..30 independent test cases')
    allowed={(m.upper(),p) for p,item in contract['openapi'].get('paths',{}).items()
             for m in item if m in ('get','post','put','patch','delete')}
    for case in cases:
        if not isinstance(case.get('requirement_id'),str) or not case.get('name'):
            raise ValueError('Test must identify requirement and expected behavior')
        if role=='BACKEND':
            if not case.get('steps'):
                raise ValueError('HTTP test needs steps')
            for step in case['steps']:
                path=step.get('path','')
                if not path.startswith('/') or path.startswith('//') or '://' in path:
                    raise ValueError('Only relative HTTP paths allowed')
                if step.get('method') not in ('GET','POST','PUT','PATCH','DELETE'):
                    raise ValueError('Invalid HTTP method')
                if not isinstance(step.get('status'),int):
                    raise ValueError('Explicit expected status required')
                if not any(m==step['method'] and re.fullmatch(re.sub(r'\{[^}]+\}',r'[^/]+',p),path.split('?')[0]) for m,p in allowed):
                    raise ValueError('Test targets endpoint outside approved OpenAPI')
                contains=step.get('json_contains') or {}
                if not isinstance(contains,dict) or any(v in ('',None) for v in contains.values()):
                    raise ValueError('json_contains needs concrete expected values, not empty placeholders')
        elif role=='WEB':
            if not str(case.get('path','')).startswith('/') or case['path'].startswith('//'):
                raise ValueError('Relative browser route required')
            if not case.get('visible_text'):
                raise ValueError('Browser test needs observable assertion')
            actions=case.get('actions') or []
            if not isinstance(actions,list) or len(actions)>10:
                raise ValueError('actions must be a list of at most 10 steps')
            for a in actions:
                kind=set(a)&set(WEB_ACTIONS) if isinstance(a,dict) else set()
                if len(kind)!=1 or not all(isinstance(v,str) and v for v in a.values()):
                    raise ValueError('Each action is one of fill/select/click with text values')
                if 'fill' in a and 'value' not in a or 'select' in a and 'option' not in a:
                    raise ValueError('fill needs value, select needs option')
        elif not case.get('visible_text'):
            raise ValueError('Android test needs visible text assertion')
    return cases

async def make_tests(data, evidence):
    role=data['role']
    schema={'BACKEND':'[{"name":"scenario","requirement_id":"AC-1","steps":['
                      '{"method":"POST","path":"/api/items","body":{"name":"Item"},"status":201,"json_contains":{"name":"Item"}},'
                      '{"method":"GET","path":"/api/items/{id}","status":200,"json_contains":{},"json_schema":{}}]}]. '
                      'POST/PUT/PATCH steps must send "body" matching the contract requestBody. '
                      'A placeholder like {id} in a path or body value is filled from the same-named field of the previous '
                      'step JSON response; {step1.id} takes it from the response of step 1 (use it when the needed object '
                      'was created earlier than the previous step, e.g. the author id after creating a book). '
                      'Never type out long strings: for a value longer than 30 characters write {"$repeat":"a","times":101}. '
                      'json_contains holds only exact values you know in advance (e.g. what you sent); on an array response '
                      'it passes when some item has those fields. Omit it for fields you cannot predict (ids, dates, '
                      'error message wording) — the frozen OpenAPI response schema is checked anyway. '
                      'Absence of an item cannot be asserted: verify deletion by the status code. '
                      'For GET /health assert only status 200, no body fields',
            'WEB':'[{"name":"screen","requirement_id":"AC-1","path":"/","visible_text":"expected text"},'
                  '{"name":"behaviour","requirement_id":"AC-2","path":"/","actions":[{"fill":"<field label>","value":"5"},'
                  '{"select":"<field label>","option":"<option text>"},{"click":"<button caption>"}],"visible_text":"expected result"}]. '
                  'visible_text is ONE exact UI string (a heading, a field label, a button caption, a result); '
                  'write a separate case for each string, never join several strings with commas. '
                  'The page starts with an EMPTY data set and the backend may be absent: never assert texts that appear '
                  'per data item (e.g. a delete button in a row) and do not use actions that need server data. '
                  'Use actions only for logic that runs in the browser, when the requirements define the result exactly; '
                  'take field labels, options and button captions verbatim from the requirements. '
                  'For a numeric result assert only digits that do not depend on formatting (e.g. "1.609" for 1 mile in km '
                  'with 3 decimals; avoid thousands separators)',
            'MOBILE':'[{"name":"screen","requirement_id":"AC-1","visible_text":"expected text"}]'}[role]
    spent=0;problem=None;best=None
    criteria={a['id'] for a in data['contract'].get('acceptance_criteria',[]) if role in a.get('roles',[])}
    for attempt in range(3):
        try:
            payload={'requirements':data['requirements'],'contract':data['contract'],'startup':evidence}
            if problem:
                payload['previous_attempt_problem']=problem
            cases,tokens=await llm('You are an independent QA engineer. Application STARTUP HAS PASSED. '
                'Design behavioral acceptance tests from requirements and immutable contract. Return only JSON matching '+schema+
                '. Use only approved endpoints. Expected values must come from business requirements. '
                'Write at least one test for EVERY acceptance criterion assigned to this component '
                '(its id in requirement_id); for a criterion about storage, test the observable effect '
                '(create, then read back). Do not claim coverage beyond actual assertions. No shell commands. '
                'Each test is independent.',payload)
            spent+=tokens
            cases=validate_cases(cases,role,data['contract'])
        except ValueError as e:
            # malformed/truncated LLM JSON is a tester failure, not a verdict on the application
            problem=f'Invalid test list: {e}'
            if attempt==2 and best is None:
                raise
            continue
        missing=sorted(criteria-{c['requirement_id'] for c in cases})
        if best is None or len(missing)<best[1]:
            best=(cases,len(missing))
        if not missing:
            break
        problem='No tests for acceptance criteria '+', '.join(missing)+'. Add tests for them, keep the others.'
    # still uncovered after retries: run what exists, coverage.json marks the rest unverified
    return best[0],spent

def nullable(schema):
    """OpenAPI 3.0 `nullable: true` -> JSON Schema type list with "null" (jsonschema ignores nullable)."""
    if isinstance(schema,list):
        return [nullable(s) for s in schema]
    if not isinstance(schema,dict):
        return schema
    out={k:nullable(v) for k,v in schema.items()}
    if out.pop('nullable',False) is True:
        if isinstance(out.get('type'),str):
            out['type']=[out['type'],'null']
        elif 'type' not in out:
            out={'anyOf':[out,{'type':'null'}]}
    return out

PLACEHOLDER=re.compile(r'\{(\w+(?:\.\w+)?)\}')

def substitute(value,saved):
    """Fill {field} / {stepN.field} from earlier responses; a whole-string placeholder keeps the value type."""
    if isinstance(value,str):
        whole=PLACEHOLDER.fullmatch(value)
        if whole and whole.group(1) in saved:
            return saved[whole.group(1)]
        return PLACEHOLDER.sub(lambda m:str(saved.get(m.group(1),m.group(0))),value)
    if isinstance(value,dict):
        return {k:substitute(v,saved) for k,v in value.items()}
    if isinstance(value,list):
        return [substitute(v,saved) for v in value]
    return value

def expand(value):
    """Expand {"$repeat": "a", "times": N} macros used for long test strings."""
    if isinstance(value,dict):
        if set(value)=={'$repeat','times'}:
            return str(value['$repeat'])*min(int(value['times']),100_000)
        return {k:expand(v) for k,v in value.items()}
    if isinstance(value,list):
        return [expand(v) for v in value]
    return value

async def http_tests(base,cases,openapi=None):
    results=[]
    openapi=nullable(openapi) if openapi else openapi
    async with httpx.AsyncClient(timeout=30) as c:
        for case in cases:
            try:
                saved={}
                for number,step in enumerate(case['steps'],1):
                    # chain steps: {id} — field of the previous JSON response, {step1.id} — of step 1
                    path=substitute(step['path'],saved)
                    if '{' in path:
                        raise ValueError(f'Unresolved path placeholder {path}; earlier responses lack the field')
                    r=await c.request(step['method'],base+path,json=substitute(expand(step.get('body')),saved))
                    try:
                        data=r.json()
                        if isinstance(data,dict):
                            fields={k:v for k,v in data.items() if isinstance(v,(str,int))}
                            saved.update(fields)
                            saved.update({f'step{number}.{k}':v for k,v in fields.items()})
                    except ValueError:
                        pass
                    if r.status_code!=step['status']:
                        raise ValueError(f"{step['method']} {path}: expected {step['status']}, actual {r.status_code}: {r.text[:1500]}")
                    if openapi:
                        route=next((p for p in openapi['paths'] if re.fullmatch(re.sub(r'\{[^}]+\}',r'[^/]+',p),path.split('?')[0])),None)
                        operation=openapi['paths'].get(route,{}).get(step['method'].lower(),{})
                        response=operation.get('responses',{}).get(str(r.status_code),operation.get('responses',{}).get('default',{}))
                        if not response:
                            raise ValueError('Response status absent from frozen OpenAPI')
                        schema=response.get('content',{}).get('application/json',{}).get('schema')
                        if schema:
                            jsonschema.validate(r.json(),schema,resolver=jsonschema.RefResolver.from_schema(openapi))
                    if step.get('json_contains') or step.get('json_schema'):
                        body=r.json()
                        expected=step.get('json_contains') or {}
                        def matches(item):
                            return isinstance(item,dict) and all(item.get(k)==v for k,v in expected.items())
                        # object: fields equal; array: some item has these fields
                        if expected and not (matches(body) or isinstance(body,list) and any(map(matches,body))):
                            raise ValueError(f'Expected fields {expected!r}, actual {str(body)[:1500]}')
                        if step.get('json_schema'):
                            jsonschema.validate(body,nullable(step['json_schema']))
                results.append({**case,'passed':True})
            except (ValueError,httpx.HTTPError,jsonschema.ValidationError) as e:
                results.append({**case,'passed':False,'error':str(e)[:3000]})
    return results

WEB_ACTIONS=('fill','select','click')
NUMBER=re.compile(r'-?\d[\d\s  ]*(?:[.,]\d+)?')

async def perform(page,action):
    """One user step found by visible text: field label/placeholder, option text, button caption."""
    if 'fill' in action:
        field=page.get_by_label(action['fill'],exact=False).or_(page.get_by_placeholder(action['fill'],exact=False))
        await field.filter(visible=True).first.fill(action['value'],timeout=10000)
    elif 'select' in action:
        field=page.get_by_label(action['select'],exact=False).filter(visible=True).first
        if await field.evaluate('el => el.tagName')=='SELECT':
            await field.select_option(label=action['option'],timeout=10000)
        else:  # Angular Material and other custom dropdowns: open, then pick the option
            await field.click(timeout=10000)
            await page.get_by_role('option',name=action['option'],exact=True).filter(visible=True).first.click(timeout=10000)
    else:
        button=page.get_by_role('button',name=action['click']).or_(page.get_by_text(action['click'],exact=True))
        await button.filter(visible=True).first.click(timeout=10000)

async def browser_tests(base,cases):
    from playwright.async_api import async_playwright
    results=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox'])
        for case in cases:
            page=await browser.new_page()
            errors=[]  # runtime errors explain "element not found" better than the locator timeout
            page.on('pageerror',lambda e,errors=errors:errors.append(str(e)[:400]))
            page.on('console',lambda m,errors=errors:m.type=='error' and errors.append(m.text[:400]))
            try:
                await page.goto(base+case['path'],wait_until='networkidle',timeout=30000)
                for number,action in enumerate(case.get('actions') or [],1):
                    try:
                        await perform(page,action)
                    except Exception as e:
                        raise RuntimeError(f'step {number} {action}: {str(e)[:500]}') from e
                if NUMBER.fullmatch(case['visible_text'].strip()):
                    # numbers: "1 609,344" (ru locale) and "1609.344" are the same result
                    # whole number only: "1.609" must not match inside "11.6099"
                    await page.wait_for_function(
                        "e => new RegExp('(^|[^0-9.])' + e.replace(/\\./g,'\\\\.') + '(?![0-9])')"
                        ".test(document.body.innerText.replace(/[\\s\\u00a0\\u202f]/g,'').replace(/,/g,'.'))",
                        arg=re.sub(r'[\s  ]','',case['visible_text']).replace(',','.'),timeout=15000)
                else:
                    # first VISIBLE match: the same text may also sit in hidden nodes (empty table header, error hint)
                    await page.get_by_text(case['visible_text'],exact=False).filter(visible=True).first.wait_for(timeout=15000)
                results.append({**case,'passed':True})
            except Exception as e:
                console=('\nОшибки в консоли браузера (вероятная причина): '+' | '.join(dict.fromkeys(errors))[:1500]) if errors else ''
                results.append({**case,'passed':False,'error':(console+'\n'+str(e))[:3000] if console else str(e)[:3000]})
            finally:
                await page.close()
        await browser.close()
    return results

async def mobile_validate(data, source, report):
    # Runner image must contain Android SDK, emulator and Gradle. No device -> rejected startup.
    image=os.getenv('ANDROID_RUNNER_IMAGE','mas-android-runner')
    name='mas-mobile-'+uuid4().hex[:12]
    runtime=report.parent/('mobile-runtime-'+str(data['attempt']))
    shutil.copytree(source,runtime,dirs_exist_ok=True)
    script=Path('/app/mobile_runner.sh').read_text()
    (runtime/'qa-run.sh').write_text(script)
    try:
        built=await command(['docker','run','-d','--name',name,'--device','/dev/kvm',
          '-v',os.getenv('DATA_VOLUME','mas-data')+':/app/output','-w',str(runtime),image,'bash','qa-run.sh'],60)
        if not built['ok']:
            return {'status':'rejected','stage':'startup','error':built['output']}
        marker=runtime/'startup.json'
        for _ in range(600):
            if marker.exists():
                break
            state=await command(['docker','inspect','--format','{{.State.Running}}',name],15)
            if state['output'].strip()!='true':
                break
            await asyncio.sleep(2)
        logs=await command(['docker','logs',name],30)
        if not marker.exists():
            return {'status':'rejected','stage':'startup','error':logs['output']}
        cases,tokens=await make_tests(data,json.loads(marker.read_text()))
        results=[]
        for case in cases:
            r=await command(['docker','exec',name,'bash','-lc',
                'adb shell uiautomator dump /sdcard/ui.xml >/dev/null && adb shell cat /sdcard/ui.xml'],30)
            results.append({**case,'passed':r['ok'] and case['visible_text'] in r['output']})
        for rel in ('gradlew','gradlew.bat','gradle/wrapper/gradle-wrapper.jar'):
            p=runtime/rel
            if p.exists():
                dest=source/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
        return {'status':'passed' if all(r['passed'] for r in results) else 'failed',
                'startup':True,'cases':results,'tokens':tokens}
    finally:
        await command(['docker','rm','-f',name],30)

async def validate(data):
    rid=str(UUID(data['run_id']));role=data['role']
    if role not in ('BACKEND','WEB','MOBILE'):
        raise ValueError('Unknown role')
    source=contained(ROOT/rid,Path(data['source']))
    if source != ROOT/rid/role.lower():
        raise ValueError('Unexpected source folder')
    report=ROOT/rid/'qa'/role.lower()/f"attempt-{int(data['attempt'])}.json"
    report.parent.mkdir(parents=True,exist_ok=True)
    if role=='MOBILE':
        outcome=await mobile_validate(data,source,report)
    else:
        name='mas-qa-'+uuid4().hex[:12]
        image=name+':test';db=name+'-db'
        try:
            built=await command(['docker','build','-t',image,str(source)])
            if not built['ok']:
                outcome={'status':'rejected','stage':'build','error':built['output']}
            else:
                env=[]
                if role=='BACKEND':
                    db_start=await command(['docker','run','-d','--name',db,'--network',NETWORK,
                        '-e','POSTGRES_DB=app','-e','POSTGRES_USER=app','-e','POSTGRES_PASSWORD=app',
                        'postgis/postgis:17-3.5'])
                    if not db_start['ok']:
                        raise RuntimeError(db_start['output'])
                    for _ in range(45):
                        ready=await command(['docker','exec',db,'pg_isready','-U','app'],10)
                        if ready['ok']: break
                        await asyncio.sleep(2)
                    env=['-e',f'ConnectionStrings__Default=Host={db};Port=5432;Database=app;Username=app;Password=app']
                start=await command(['docker','run','-d','--name',name,'--network',NETWORK,*env,image])
                port=8080 if role=='BACKEND' else 80
                base=f'http://{name}:{port}'
                started=start['ok'] and await wait_http(base+('/health' if role=='BACKEND' else '/'))
                if not started:
                    logs=await command(['docker','logs',name],30)
                    outcome={'status':'rejected','stage':'startup','error':logs['output']}
                else:
                    cases,tokens=await make_tests(data,{'url':base,'status':200})
                    save(report.with_suffix('.tests.json'),cases)
                    results=await (http_tests(base,cases,data['contract']['openapi']) if role=='BACKEND' else browser_tests(base,cases))
                    outcome={'status':'passed' if results and all(r['passed'] for r in results) else 'failed',
                             'startup':True,'cases':results,'tokens':tokens,
                             'scope':'component; cross-component integration still required'}
        finally:
            await command(['docker','rm','-f',name,db],30)
            await command(['docker','image','rm',image],30)
    if outcome['status']=='passed':
        if role=='BACKEND':
            actual=source/'openapi.yaml'
            if not actual.exists():
                outcome={'status':'failed','stage':'contract','error':'Backend did not emit OpenAPI'}
            else:
                import yaml
                observed=yaml.safe_load(actual.read_text())
                expected=data['contract']['openapi']
                # compare wire shapes, not summaries or operation identifiers
                def wire(api):
                    return {p:{m:{k:v for k,v in op.items() if k in ('parameters','requestBody','responses')}
                               for m,op in methods.items() if m in ('get','post','put','patch','delete')}
                            for p,methods in api.get('paths',{}).items()}
                if wire(observed)!=wire(expected) or observed.get('components',{})!=expected.get('components',{}):
                    outcome={'status':'failed','stage':'contract',
                             'error':'Generated backend OpenAPI differs from immutable contract',
                             'expected':expected,'actual':observed}
    if outcome['status']=='passed':
        zip_path=ROOT/rid/'approved'/(role.lower()+'.zip')
        zip_path.parent.mkdir(parents=True,exist_ok=True)
        archive(source,zip_path)
        outcome.update(zip_path=str(zip_path),contract_hash=digest(data['contract']))
    else:
        outcome['feedback']={'errors':[{'message':outcome.get('error','Acceptance tests failed'),
            'fix_hint':json.dumps(outcome,ensure_ascii=False)[:22000]}]}
    save(report,outcome)
    return outcome

@app.get('/health')
def health(): return {'status':'ok'}

@app.post('/validate')
async def endpoint(data:dict):
    try:
        return await validate(data)
    except Exception as e:
        # infrastructure and test generation failures never approve an artifact
        return {'status':'error','stage':'tester','error':str(e)}

@app.post('/integrate')
async def integrate(data:dict):
    rid=str(UUID(data['run_id']));roles=data['roles'];product=ROOT/rid/'product'
    if set(roles)=={'MOBILE'}:
        return {'status':'passed','scope':'mobile component only; no server integration requested'}
    network='mas-int-'+uuid4().hex[:12]
    names=[];images=[];evidence=[]
    tester=os.environ.get('HOSTNAME','')
    try:
        created=await command(['docker','network','create',network],30)
        if not created['ok']: raise RuntimeError(created['output'])
        connected=await command(['docker','network','connect',network,tester],30)
        if not connected['ok']: raise RuntimeError(connected['output'])
        if 'BACKEND' in roles:
            db=network+'-db';names.append(db)
            result=await command(['docker','run','-d','--name',db,'--network',network,'--network-alias','db',
                '-e','POSTGRES_DB=app','-e','POSTGRES_USER=app','-e','POSTGRES_PASSWORD=app','postgis/postgis:17-3.5'])
            if not result['ok']: raise RuntimeError(result['output'])
            for _ in range(45):
                check=await command(['docker','exec',db,'pg_isready','-U','app'],10)
                if check['ok']: break
                await asyncio.sleep(2)
        addresses={}
        for role in ('BACKEND','WEB'):
            if role not in roles: continue
            name=network+'-'+role.lower();image=name+':int'
            names.append(name);images.append(image)
            built=await command(['docker','build','-t',image,str(product/role.lower())])
            if not built['ok']: raise RuntimeError(built['output'])
            env=['-e','ConnectionStrings__Default=Host=db;Database=app;Username=app;Password=app'] if role=='BACKEND' else []
            result=await command(['docker','run','-d','--name',name,'--network',network,
                '--network-alias',role.lower(),*env,image])
            base=f'http://{name}:'+('8080' if role=='BACKEND' else '80')
            if not result['ok'] or not await wait_http(base+('/health' if role=='BACKEND' else '/')):
                logs=await command(['docker','logs',name],30);raise RuntimeError(logs['output'])
            addresses[role]=base
        if 'BACKEND' in roles:
            reports=attempt_reports(ROOT/rid/'qa/backend')
            qa=json.loads(reports[-1].read_text())
            results=await http_tests(addresses['BACKEND'],qa['cases'],data['contract']['openapi']);evidence+=results
            if 'WEB' in roles:
                # validate the same real backend through web nginx /api proxy
                proxy_cases=[{**c,'steps':[s for s in c['steps'] if s['path'].startswith('/api/')]} for c in qa['cases']]
                proxy_cases=[c for c in proxy_cases if c['steps']]
                if not proxy_cases: raise ValueError('No API test available to verify web/backend connection')
                evidence+=await http_tests(addresses['WEB'],proxy_cases,data['contract']['openapi'])
        if 'WEB' in roles:
            reports=attempt_reports(ROOT/rid/'qa/web')
            qa=json.loads(reports[-1].read_text())
            evidence+=await browser_tests(addresses['WEB'],qa['cases'])
        return {'status':'passed' if evidence and all(r['passed'] for r in evidence) else 'failed',
                'cases':evidence,'scope':'backend HTTP and web browser/proxy integration; mobile device component tested separately'}
    except Exception as e:
        return {'status':'failed','error':str(e)}
    finally:
        if names: await command(['docker','rm','-f',*names],60)
        if images: await command(['docker','image','rm',*images],60)
        await command(['docker','network','disconnect','-f',network,tester],30)
        await command(['docker','network','rm',network],30)

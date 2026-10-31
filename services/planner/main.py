"""Планировщик: ТЗ -> выбранные компоненты, их задачи и замороженный контракт.

POST /plan {"spec": {...}} -> {"plan": {"tasks": [...], "contract": {...}}, "tokens": int}
Невалидный план -> 422 с причиной.
"""

import httpx
from fastapi import FastAPI, HTTPException
from common import llm

app=FastAPI(title='Planner')
ROLES={'BACKEND','WEB','MOBILE'}

@app.get('/health')
def health(): return {'status':'ok','service':'planner'}

async def plan(spec):
    result,tokens=await llm('You are the central architect of a universal product-generation pipeline. '
      'Return JSON {"tasks":[{"role":"BACKEND|WEB|MOBILE","task":"precise instruction",'
      '"context":{}}],"contract":{"openapi":{full OpenAPI 3.0.3},"events":[],"data":{},'
      '"runtime":{},"acceptance_criteria":[{"id":"AC-1","text":"...","roles":["BACKEND"]}]}}. '
      'Select ONLY required components. BACKEND ONLY means one BACKEND task, WEB + BACKEND excludes MOBILE. '
      'Use ASP.NET Core 10 C#14, Angular20.1.6, Kotlin2.2.21. Include GET /health with response 200 '
      'if backend selected. API schemas must be concrete JSON schemas with types and required properties. '
      'All clients must use the same field names and URLs. Every criterion has a stable ID. '
      'Copy every acceptance criterion from the specification VERBATIM, in its original language: never translate '
      'or paraphrase it, keep quoted UI texts and numbers exactly. Write task and context in the language of the '
      'specification too. '
      'Context is domain-specific but contains all necessary requirements. Freeze all external contracts. '
      'For standalone mobile/web, OpenAPI paths may be empty. Runtime ports backend8080 web80. '
      'External integrations use adapters and mocks.',spec)
    tasks=result.get('tasks',[])
    roles=[t.get('role') for t in tasks]
    if not roles or len(set(roles))!=len(roles) or not set(roles)<=ROLES:
        raise ValueError('Invalid component selection')
    c=result['contract']
    if not isinstance(c.get('openapi',{}).get('paths'),dict):
        raise ValueError('Missing OpenAPI paths')
    if not c.get('acceptance_criteria'):
        raise ValueError('Missing acceptance criteria')
    for t in tasks:
        if not t.get('task') or not isinstance(t.get('context'),dict):
            raise ValueError('Invalid task/context')
    return result,tokens

def drop_disabled(result,disabled):
    """Disabled components are planned (full contract and criteria) but get no task."""
    skipped=[t['role'] for t in result['tasks'] if t['role'] in disabled]
    result['tasks']=[t for t in result['tasks'] if t['role'] not in disabled]
    result['disabled']=skipped
    if not result['tasks']:
        raise ValueError('Запрос требует только отключённые компоненты ('+', '.join(skipped)
                         +'); уберите их из DISABLED_ROLES оркестратора или измените запрос')
    return result

@app.post('/plan')
async def endpoint(data:dict):
    try:
        result,tokens=await plan(data['spec'])
        result=drop_disabled(result,set(data.get('disabled_roles') or []))
    except (ValueError,KeyError) as e:
        raise HTTPException(422,str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502,f'LLM unavailable: {e!r}')
    return {'plan':result,'tokens':tokens}

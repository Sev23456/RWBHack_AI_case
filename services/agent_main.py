import asyncio
import json
import os
from pathlib import Path
from uuid import UUID
from fastapi import FastAPI, HTTPException
from common import ROOT, archive, contained, digest, request, save

ROLE=os.environ['AGENT_ROLE']
app=FastAPI(title=ROLE+' agent')
lock=asyncio.Lock()  # original modules keep global usage/debug state

@app.get('/health')
def health():
    return {'status':'ok','role':ROLE}

async def generate(task, context, folder):
    if ROLE == 'MOBILE':
        from mobile_agent import mobile_agent
        result=await mobile_agent({'task':task,'context':json.dumps(context,ensure_ascii=False)})
        for name, text in result['files'].items():
            p=contained(folder,name);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
        return {'status':'ok','tokens':result['tokens']}
    import teamlead
    return await teamlead.run(task,context)

@app.post('/work')
async def work(data:dict):
    run_id=str(UUID(data['run_id']))
    folder=ROOT/run_id/ROLE.lower()
    contract=data['contract']
    contract_file=ROOT/run_id/'contracts/openapi.json'
    save(contract_file,contract['openapi'])
    context={'requirements':data['context'],'contract':contract,
             'contract_path':str(contract_file.relative_to(Path.cwd())),
             'output_dir':str(folder.relative_to(Path.cwd())),
             'frontend_dir':str(folder.relative_to(Path.cwd())),
             'backend_url':'http://backend:8080','max_files':30}
    task=data['task']+'\nСоблюдай переданный контракт точно. Не добавляй внешние API вне контракта.'
    reports=[]
    async with lock:
        for attempt in range(1,int(os.getenv('MAX_REPAIR_ATTEMPTS','3'))+1):
            try:
                generated=await generate(task,context,folder)
                # direct agent -> tester call; orchestrator only receives final outcome
                result=await request(os.environ['TESTER_URL']+'/validate',{
                    'run_id':run_id,'role':ROLE,'source':str(folder),
                    'contract':contract,'requirements':data['context'],
                    'attempt':attempt,'generation':generated})
                reports.append({'attempt':attempt,'generation':generated,'test':result})
                save(folder.parent/(ROLE.lower()+'-attempts.json'),reports)
                if result['status']=='passed':
                    return {'role':ROLE,'status':'passed','zip_path':result['zip_path'],
                            'contract_hash':digest(contract),'attempts':attempt,
                            'tokens':sum(x['generation'].get('tokens',0)+x['test'].get('tokens',0) for x in reports)}
                feedback=result.get('feedback',{'errors':[{'message':result.get('error','QA failed')}]})
                context.update(mode='fix',feedback=feedback)
                if ROLE=='MOBILE':
                    task+='\nИсправь по отчёту тестера:\n'+json.dumps(feedback,ensure_ascii=False)
            except (ValueError,RuntimeError,OSError) as e:
                return {'role':ROLE,'status':'failed','error':str(e),'attempts':attempt}
    return {'role':ROLE,'status':'failed','error':'Repair attempts exhausted','reports':reports}

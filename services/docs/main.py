"""Агент документации: документация по фактическому результату прогона.

POST /document {"spec", "contract", "files", "integration", "coverage"}
  -> {"docs": {"README.md": "...", ...}, "tokens": int}
Ответ модели не в виде {имя файла: текст} -> 422.
"""

import httpx
from fastapi import FastAPI, HTTPException
from common import llm

app=FastAPI(title='Documentation agent')

@app.get('/health')
def health(): return {'status':'ok','service':'docs'}

@app.post('/document')
async def document(data:dict):
    try:
        docs,tokens=await llm('Return JSON {"README.md":"...","architecture.md":"...",'
           '"user-guide.md":"..."}. Write Russian documentation for the actual generated product. '
           'Use supplied real file paths and docker-compose.yml. Clearly distinguish tested behavior from unverified requirements.',
           data)
    except ValueError as e:
        raise HTTPException(422,str(e))
    except httpx.HTTPError as e:
        raise HTTPException(502,f'LLM unavailable: {e!r}')
    if not isinstance(docs,dict) or not docs or not all(isinstance(k,str) and isinstance(v,str) for k,v in docs.items()):
        raise HTTPException(422,'Documentation must be {file name: text}')
    return {'docs':docs,'tokens':tokens}

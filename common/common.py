import asyncio
import hashlib
import io
import json
import os
import uuid
import zipfile
from pathlib import Path
import httpx

ROOT = Path(os.getenv('OUTPUT_ROOT', '/app/output')).resolve()
ROOT.mkdir(parents=True, exist_ok=True)

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # unique tmp name: parallel agents may save the same file (e.g. contracts/openapi.json)
    tmp = path.with_suffix(f'{path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)

def contained(root, path):
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Path outside permitted directory')
    return target

def attempt_reports(folder, pattern='attempt-*.json'):
    """QA reports attempt-N.json in attempt order; skips the attempt-N.tests.json case files."""
    found=[p for p in Path(folder).glob(pattern) if p.stem.split('-')[-1].isdigit()]
    return sorted(found,key=lambda p:int(p.stem.split('-')[-1]))

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

async def request(url, payload):
    async with httpx.AsyncClient(timeout=float(os.getenv('SERVICE_TIMEOUT', '14400'))) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()

LLM_RATE_RETRIES = int(os.getenv('LLM_RATE_RETRIES', '8'))  # 429: ключ общий на всех агентов, лимит поминутный

async def llm(system, payload):
    for attempt in range(LLM_RATE_RETRIES + 1):
        async with httpx.AsyncClient(timeout=900) as c:
            r = await c.post(os.environ['LLM_URL'].rstrip('/') + '/chat/completions',
                headers={'Authorization': 'Bearer '+os.getenv('LLM_API_KEY','')},
                json={'model': os.getenv('LLM_MODEL','qwen3.8-27b'),
                      'messages':[{'role':'system','content':system},
                                  {'role':'user','content':json.dumps(payload,ensure_ascii=False)}],
                      'temperature':0.1,'max_tokens':24000,
                      'chat_template_kwargs':{'enable_thinking':False}})
        if r.status_code == 429 and attempt < LLM_RATE_RETRIES:
            await asyncio.sleep(min(float(r.headers.get('Retry-After',30)),60))
            continue
        r.raise_for_status()
        data=r.json()
        if data['choices'][0].get('finish_reason') == 'length':
            raise ValueError('LLM response truncated')
        raw=data['choices'][0]['message']['content'].rsplit('</think>',1)[-1].strip()
        if raw.startswith('```'):
            raw=raw.split('\n',1)[1].rsplit('```',1)[0]
        return json.loads(raw), data.get('usage',{}).get('total_tokens',0)

def unpack(data, target):
    target=Path(target)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 150_000_000:
            raise ValueError('Archive too large')
        for info in z.infolist():
            p=contained(target,info.filename)
            if info.is_dir():
                p.mkdir(parents=True,exist_ok=True)
            else:
                p.parent.mkdir(parents=True,exist_ok=True)
                p.write_bytes(z.read(info))
                if (info.external_attr >> 16) & 0o111:
                    p.chmod(0o755)

def archive(source, destination):
    excluded={'node_modules','bin','obj','.angular','.gradle','build','dist','debug','__pycache__'}
    with zipfile.ZipFile(destination,'w',zipfile.ZIP_DEFLATED) as z:
        for p in sorted(Path(source).rglob('*')):
            if p.is_file() and not excluded.intersection(p.relative_to(source).parts):
                z.write(p,p.relative_to(source).as_posix())

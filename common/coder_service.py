"""Кодер (developer) роли как отдельный сервис.

Тимлид агента вызывает его через coder_client: POST /call {"fn", "args", "kwargs", "settings", "usage", "debug_dir"}.
Сервис подставляет состояние тимлида (статистику токенов, папку отладки, настройки модуля),
выполняет функцию developer и возвращает {"result", "usage"} — учёт токенов в отчёте агента не меняется.
"""

import asyncio
import copy

from fastapi import FastAPI, HTTPException

REMOTE_FUNCTIONS = ("write_file", "fix_file")


def build_app(role: str, developer, llm) -> FastAPI:
    app = FastAPI(title=f'{role} coder')
    lock = asyncio.Lock()  # состояние модулей глобальное: один вызов за раз

    @app.get('/health')
    def health():
        return {'status': 'ok', 'role': role, 'service': 'coder', 'functions': list(REMOTE_FUNCTIONS)}

    @app.post('/call')
    async def call(data: dict):
        fn = data.get('fn')
        if fn not in REMOTE_FUNCTIONS:
            raise HTTPException(400, f'unknown function {fn!r}')
        async with lock:
            for name, value in (data.get('settings') or {}).items():
                if not hasattr(developer, name):
                    raise HTTPException(400, f'unknown setting {name!r}')
                setattr(developer, name, value)
            llm.USAGE.clear()
            llm.USAGE.update(copy.deepcopy(data.get('usage') or {}))
            llm.set_debug_dir(data.get('debug_dir'))
            try:
                result = await getattr(developer, fn)(*data.get('args', []), **data.get('kwargs', {}))
            finally:
                llm.set_debug_dir(None)
            return {'result': result, 'usage': llm.USAGE}

    return app

"""Клиент кодера: тимлид подменяет им модуль developer, если задан CODER_URL.

write_file / fix_file уходят в сервис кодера (coder_service), всё остальное
(check_file, константы) берётся из локального модуля. Атрибуты, которые тимлид
выставляет модулю (например JSON_NAMING), передаются с каждым вызовом.
"""

import os

import httpx

from coder_service import REMOTE_FUNCTIONS

CODER_TIMEOUT = float(os.getenv('CODER_TIMEOUT', '3600'))  # ответ модели + ожидание лимита запросов


class RemoteDeveloper:
    def __init__(self, url: str, local, llm):
        object.__setattr__(self, '_url', url.rstrip('/'))
        object.__setattr__(self, '_local', local)
        object.__setattr__(self, '_llm', llm)
        object.__setattr__(self, '_settings', {})

    def __getattr__(self, name):
        if name in REMOTE_FUNCTIONS:
            async def remote(*args, **kwargs):
                return await self._call(name, args, kwargs)
            return remote
        return getattr(self._local, name)

    def __setattr__(self, name, value):
        setattr(self._local, name, value)
        self._settings[name] = value

    async def _call(self, fn, args, kwargs):
        debug = self._llm._DEBUG_DIR
        payload = {'fn': fn, 'args': list(args), 'kwargs': kwargs, 'settings': self._settings,
                   'usage': self._llm.USAGE, 'debug_dir': str(debug.resolve()) if debug else None}
        try:
            async with httpx.AsyncClient(timeout=CODER_TIMEOUT) as client:
                r = await client.post(f'{self._url}/call', json=payload)
        except httpx.HTTPError as e:
            raise RuntimeError(f'кодер недоступен по {self._url}: {e!r}') from e
        if r.status_code != 200:
            raise RuntimeError(f'кодер ответил {r.status_code}: {r.text[:2000]}')
        data = r.json()
        self._llm.USAGE.clear()  # тот же объект, что импортирован тимлидом
        self._llm.USAGE.update(data['usage'])
        return data['result']

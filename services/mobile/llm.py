"""Клиент Qwen, общий для всех агентов контейнера."""

import asyncio
import os

import httpx

# Настройки Qwen передаются через переменные окружения при docker run
LLM_URL = os.getenv("LLM_URL", "http://10.10.10.10:4000/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")  # ключ — только через env, не в коде!
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.8-27b")
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "900"))  # длинные ответы (контракт, код) идут минутами
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "16000"))  # чтобы модель не зацикливалась в рассуждениях
# Прокси ограничивает ключ (параллельные запросы, запросы и токены в минуту) — лимит общий на всех агентов
LLM_MAX_PARALLEL = int(os.getenv("LLM_MAX_PARALLEL", "3"))
LLM_RATE_RETRIES = int(os.getenv("LLM_RATE_RETRIES", "5"))  # сколько раз переждать 429

_parallel = asyncio.Semaphore(LLM_MAX_PARALLEL)


async def ask_llm(prompt: str, system: str = "You are a senior software engineer.", think: bool = True) -> dict:
    """Один запрос к Qwen. Возвращает текст ответа, число токенов и truncated (обрезан по max_tokens).

    think=False отключает рассуждения Qwen3 — для длинных ответов, где они только тратят токены.
    На 429 ждёт Retry-After и повторяет до LLM_RATE_RETRIES раз, потом — ошибка.
    """
    for attempt in range(LLM_RATE_RETRIES + 1):
        async with _parallel:
            async with httpx.AsyncClient(timeout=LLM_TIMEOUT, verify=False) as client:
                r = await client.post(
                    f"{LLM_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                    json={
                        "model": LLM_MODEL,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.2,
                        "max_tokens": LLM_MAX_TOKENS,
                        "chat_template_kwargs": {"enable_thinking": think},
                    },
                )
        if r.status_code == 429 and attempt < LLM_RATE_RETRIES:
            delay = float(r.headers.get("retry-after") or 30)
            print(f"[llm] 429 ({r.headers.get('rate_limit_type', 'rate limit')}), жду {delay:.0f} с", flush=True)
            await asyncio.sleep(delay)
            continue
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        return {
            "text": choice["message"]["content"] or "",
            "tokens": data.get("usage", {}).get("total_tokens", 0),
            "truncated": choice.get("finish_reason") == "length",
        }

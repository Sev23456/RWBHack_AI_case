import os
from pathlib import Path
from openai import AsyncOpenAI

# На VM этот адрес будет доступен напрямую!
# HACKATHON_* — имена переменных из отдельного analyst-service, LLM_* — общие для пайплайна
BASE_URL = os.getenv("LLM_URL") or os.getenv("HACKATHON_API_BASE_URL") or "http://10.10.10.10:4000/v1"
API_KEY = os.getenv("LLM_API_KEY") or os.getenv("HACKATHON_API_KEY") or ""
MODEL_NAME = os.getenv("LLM_MODEL") or os.getenv("HACKATHON_MODEL_NAME") or "qwen3.8-27b"
LLM_TIMEOUT = float(os.getenv("ANALYST_LLM_TIMEOUT", "180"))  # большие ТЗ генерируются дольше минуты
SYSTEM_PROMPT = Path("prompts/analyst_system.txt").read_text(encoding="utf-8")

async def call_llm(user_text: str, feedback: str | None = None) -> str:
    """feedback — ошибки проверки предыдущего ответа: модель исправляет их, а не повторяет."""
    content = f"Бизнес-требования:\n\n{user_text}\n\nВерни ТОЛЬКО валидный JSON, без пояснений и markdown-оберток."
    if feedback:
        content += f"\n\nПредыдущий ответ не прошёл проверку схемы. Исправь именно это и верни полный JSON заново:\n{feedback}"
    try:
        client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=LLM_TIMEOUT)
        response = await client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": content}
            ],
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content
    except Exception as e:
        # Теперь здесь будет реальная ошибка сети или API, а не мок
        raise RuntimeError(f"Ошибка вызова LLM API: {str(e)}")

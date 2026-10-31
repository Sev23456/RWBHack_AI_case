import json
import os
from pydantic import ValidationError
from .schemas import ProjectRequirements
from .llm_client import call_llm

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

async def extract_validated_json(raw_text: str) -> tuple[bool, dict | None, int, str | None]:
    """
    Возвращает: (success, data, attempts, error)
    Если 3 неудачные попытки — success=False, data=None (сигнал "код 0").
    """
    last_error = None
    feedback = None  # ошибки прошлой попытки уходят модели, чтобы она их исправила

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 1. Вызов LLM
            raw_response = await call_llm(raw_text, feedback)

            # 2. Парсинг JSON
            parsed = json.loads(raw_response)

            # 3. Строгая валидация через Pydantic
            validated = ProjectRequirements.model_validate(parsed)

            return True, validated.model_dump(), attempt, None

        except json.JSONDecodeError as e:
            feedback = f"ответ — невалидный JSON: {e}"
            last_error = f"Attempt {attempt}: invalid JSON - {e}"
        except ValidationError as e:
            details = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:15]]
            feedback = "\n".join(f"- {d}" for d in details)
            last_error = f"Attempt {attempt}: schema mismatch - {e.error_count()} errors: " + "; ".join(details[:5])
        except Exception as e:
            last_error = f"Attempt {attempt}: unexpected - {str(e)[:200]}"
    
    # Все 3 попытки провалились — возвращаем "код 0"
    return False, None, MAX_RETRIES, last_error
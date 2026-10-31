"""Кодер (developer) агента BACKEND в отдельном контейнере — см. common/coder_service.py."""

import developer
import llm
from coder_service import build_app

app = build_app("BACKEND", developer, llm)

"""Регрессия analyst: прогоняет кейсы из tests/analyst_cases.json через POST /analyze.

Запуск (стек поднят с docker-compose.ports.yml, analyst на 8101):
  python scripts/test_analyst.py                 # все кейсы
  python scripts/test_analyst.py 1 3             # только кейсы с id 1 и 3
  ANALYST_URL=http://host:8101 python scripts/test_analyst.py

Для каждого кейса: успех, попытки, время, сколько историй/сущностей/эндпоинтов/критериев,
какие компоненты стека заданы. Полные ответы — в analyst_results.json.
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

URL = os.getenv("ANALYST_URL", "http://localhost:8101") + "/analyze"
cases = json.loads((Path(__file__).parent.parent / "tests" / "analyst_cases.json").read_text(encoding="utf-8"))
only = {int(a) for a in sys.argv[1:]}
results, ok = [], 0
for case in cases:
    if only and case["id"] not in only:
        continue
    started = time.time()
    request = urllib.request.Request(URL, json.dumps({"text": case["text"]}).encode("utf-8"),
                                     {"Content-Type": "application/json"})
    try:
        answer = json.loads(urllib.request.urlopen(request, timeout=900).read())
    except Exception as e:
        answer = {"success": False, "error": repr(e), "attempts": 0}
    seconds = round(time.time() - started, 1)
    data = answer.get("data") or {}
    stack = {k: v for k, v in (data.get("constraints") or {}).get("stack", {}).items() if v}
    ok += bool(answer.get("success"))
    print(f"#{case['id']:>2} {'OK  ' if answer.get('success') else 'FAIL'} {seconds:>6}s попыток {answer.get('attempts')} | "
          f"истории {len(data.get('user_stories', []))}, сущности {len(data.get('entities', []))}, "
          f"API {len(data.get('api_endpoints', []))}, критерии {len(data.get('acceptance_criteria', []))} | "
          f"{case['theme']}" + ("" if answer.get("success") else f"\n     {answer.get('error')}"))
    if answer.get("success"):
        print(f"     стек: {', '.join(stack) or '-'}")
    results.append({**case, "seconds": seconds, "answer": answer})

Path("analyst_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"\nУспешно {ok}/{len(results)}. Ответы: analyst_results.json")
sys.exit(0 if ok == len(results) else 1)

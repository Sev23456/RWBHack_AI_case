from fastapi import FastAPI, HTTPException
from .schemas import AnalyzeRequest, AnalyzeResponse
from .validators import extract_validated_json

app = FastAPI(
    title="Analyst Agent",
    description="Stage 1 of AI-pipeline: text → structured requirements JSON",
    version="1.0.0"
)

@app.get("/health")
async def health():
    return {"status": "ok", "service": "analyst"}

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_requirements(req: AnalyzeRequest):
    success, data, attempts, error = await extract_validated_json(req.text)
    
    if not success:
        # Возвращаем HTTP 200 с флагом success=false — 
        # чтобы следующий агент пайплайна получил структурированный ответ
        return AnalyzeResponse(
            success=False,
            data=None,
            attempts=attempts,
            error=f"Failed after {attempts} attempts: {error}"
        )
    
    return AnalyzeResponse(
        success=True,
        data=data,
        attempts=attempts,
        error=None
    )
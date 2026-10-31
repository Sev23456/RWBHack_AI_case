from pydantic import BaseModel, Field, ConfigDict, field_validator
from typing import List, Optional, Dict, Any

class EntityField(BaseModel):
    name: str = Field(..., description="Имя поля")
    type: str = Field(..., description="Тип данных, например 'UUID', 'GEOMETRY(POLYGON, 4326)'")
    is_pk: bool = Field(default=False)
    is_fk: bool = Field(default=False)
    nullable: bool = Field(default=True)
    description: str = Field(..., min_length=1)

class Entity(BaseModel):
    name: str
    description: str = Field(..., min_length=10) # Защита от коротких описаний
    fields: List[EntityField] = Field(..., min_length=2)

class ApiEndpoint(BaseModel):
    method: str
    path: str
    description: str
    request_body: Optional[str] = None
    response_type: str

    @field_validator('request_body')
    @classmethod
    def check_request_body(cls, v, info):
        values = info.data
        if values.get('method') in ['POST', 'PUT', 'PATCH'] and not v:
            raise ValueError("Для методов POST/PUT/PATCH поле request_body обязательно")
        return v

class KafkaTopic(BaseModel):
    topic_name: str
    description: str
    payload_schema: Dict[str, str]

class UserStory(BaseModel):
    role: str
    action: str
    so_that: str = Field(..., min_length=10) # Защита от тавтологий

class StackConstraints(BaseModel):
    backend: Optional[str] = None  # null, если компонент не нужен
    frontend: Optional[str] = None  # null, если компонент не нужен
    mobile: Optional[str] = None  # null, если компонент не нужен
    database: Optional[str] = None  # null, если компонент не нужен
    messaging: Optional[str] = None  # null, если компонент не нужен
    infrastructure: Optional[str] = None  # null, если компонент не нужен
    ci_cd: Optional[str] = None  # null, если компонент не нужен

class Constraints(BaseModel):
    stack: StackConstraints
    geo: Optional[str] = None
    external_services: List[str]

class ProjectRequirements(BaseModel):
    model_config = ConfigDict(strict=True)
    project_name: str = Field(..., min_length=1)
    domain: str = Field(..., min_length=1)
    description: str = Field(..., min_length=10) # Требование длинного описания
    
    user_stories: List[UserStory] = Field(..., min_length=1)
    entities: List[Entity] = Field(..., min_length=1) # Минимум 4 сущности
    api_endpoints: List[ApiEndpoint] = Field(default_factory=list) # пусто для web/mobile без сервера
    kafka_topics: List[KafkaTopic] = Field(default_factory=list)
    constraints: Constraints
    acceptance_criteria: List[str] = Field(..., min_length=1)

class AnalyzeRequest(BaseModel):
    text: str = Field(..., min_length=10)

class AnalyzeResponse(BaseModel):
    success: bool
    data: Optional[ProjectRequirements] = None
    attempts: int
    error: Optional[str] = None
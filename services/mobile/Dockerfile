FROM python:3.12-slim
WORKDIR /app
COPY services/mobile/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ /common/
ENV PYTHONPATH=/common
COPY services/mobile/ .
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]

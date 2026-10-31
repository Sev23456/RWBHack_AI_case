FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y curl ca-certificates libicu-dev && curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/install.sh && bash /tmp/install.sh --channel 10.0 --install-dir /usr/share/dotnet && ln -s /usr/share/dotnet/dotnet /usr/local/bin/dotnet && rm -rf /var/lib/apt/lists/*
COPY services/backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ /common/
ENV PYTHONPATH=/common
COPY services/backend/ .
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]

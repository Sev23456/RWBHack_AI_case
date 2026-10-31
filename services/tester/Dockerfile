FROM python:3.12-bookworm
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx jsonschema pyyaml playwright && playwright install --with-deps chromium
COPY common/ /common/
ENV PYTHONPATH=/common
COPY services/tester/ .
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]

FROM python:3.12-slim
WORKDIR /app
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY services/web/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common/ /common/
ENV PYTHONPATH=/common
COPY services/web/ .
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","8000"]

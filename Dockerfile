FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock && pip install --no-deps . && useradd --uid 10001 agent
USER 10001
EXPOSE 8000
CMD ["uvicorn", "mijia_agent.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

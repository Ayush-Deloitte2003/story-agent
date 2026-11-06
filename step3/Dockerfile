# One image for both pods: the app (default command) and the publisher MCP server.
FROM python:3.12-slim
WORKDIR /app
COPY requirements-kind.lock .
RUN pip install --no-cache-dir -r requirements-kind.lock
COPY app app
COPY mcp_server mcp_server
COPY skills skills
ENV PYTHONUNBUFFERED=1
EXPOSE 8000 8001 8765
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

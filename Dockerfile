FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml ./
COPY rea_bot ./rea_bot
COPY alembic.ini ./
RUN pip install --no-cache-dir . && useradd --create-home bot && mkdir /app/data && chown bot:bot /app/data
USER bot
ENTRYPOINT ["python", "-m", "rea_bot.container_entrypoint"]
CMD ["python", "-m", "rea_bot"]

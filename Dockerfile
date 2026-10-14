FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependências primeiro: aproveita o cache de camadas do Docker
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY main.py .

# Roda sem privilégios de root
RUN useradd --create-home --shell /usr/sbin/nologin appuser
USER appuser

# Render e Koyeb injetam a variável PORT em tempo de execução; 8000 é o padrão local
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT}"]

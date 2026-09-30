ARG PYTHON_IMAGE=python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
RUN groupadd --gid 10001 bot && useradd --uid 10001 --gid bot --create-home bot
COPY fishbot /app/fishbot
COPY tests /app/tests
COPY config.toml /app/config.toml
RUN mkdir /app/data /app/secrets && chown -R bot:bot /app/data /app/secrets
USER bot
ENTRYPOINT ["python", "-m", "fishbot"]
CMD ["run"]

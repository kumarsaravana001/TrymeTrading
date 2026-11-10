ARG BASE=python:3.12-slim
FROM ${BASE}
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .[dev]
COPY config ./config
COPY policies ./policies
COPY tests ./tests
RUN useradd -m -u 10001 tryme && mkdir -p /app/var && chown -R tryme /app
USER tryme
# container fails its build if the regression suite fails
RUN python -m pytest -q
ENTRYPOINT ["python", "-m", "tryme.cli"]
CMD ["run"]

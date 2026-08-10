FROM tensorzero/gateway:latest
COPY tensorzero.toml /app/config/tensorzero.toml
COPY templates/ /app/config/templates/
CMD ["--config-file", "/app/config/tensorzero.toml"]
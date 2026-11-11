FROM metacubex/mihomo:v1.19.19 AS mihomo
FROM python:3.12-alpine
COPY --from=mihomo /mihomo /mihomo
WORKDIR /app
COPY config.template.json render.py ./
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 7890
ENTRYPOINT ["python", "/app/render.py"]

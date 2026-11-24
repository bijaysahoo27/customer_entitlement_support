FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirement.txt ./
RUN pip install --no-cache-dir -r requirement.txt
COPY src ./src
RUN mkdir /data && chown 10001:10001 /data
USER 10001:10001
ENTRYPOINT ["python", "-m", "uvicorn", "--host", "0.0.0.0"]
CMD ["customer_entitlement_support:app"]

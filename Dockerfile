FROM python:3.11-alpine
WORKDIR /app
COPY kavita_prewarm.py .
RUN mkdir -p /state && chown -R 65532:65532 /app /state
USER 65532:65532
VOLUME ["/state"]
CMD ["python", "-u", "kavita_prewarm.py"]

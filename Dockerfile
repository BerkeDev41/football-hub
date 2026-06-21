FROM python:3.12-slim

WORKDIR /app
COPY app.py football_data_provider.py .

ENV PORT=8080
EXPOSE 8080

# No dependencies to install — pure standard library.
CMD ["python", "app.py"]

FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Kubernetes runs the pod as uid 1000 (see securityContext in the Deployment)
# while the base image leaves /app owned by root. SQLite must create its
# journal file next to applied.db inside /app, so the directory has to be
# writable by uid 1000 or every database write fails.
RUN mkdir -p /app && chown -R 1000:100 /app
CMD ["python", "-u", "main.py"]

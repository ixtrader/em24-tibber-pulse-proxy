FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir 'pymodbus>=3.5.0,<3.6.0'

COPY EM24-Tibber-Pulse-Proxy.py .

EXPOSE 502

ENTRYPOINT ["python3", "EM24-Tibber-Pulse-Proxy.py"]
FROM mcr.microsoft.com/azure-cli:latest

WORKDIR /work
COPY az-ddns/ /work/az-ddns/

# dns.json is mounted at /config/dns.json by the caller (see docker-compose.yml)
ENTRYPOINT ["python3", "/work/az-ddns", "--config", "/config/dns.json"]
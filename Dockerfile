FROM mcr.microsoft.com/azure-cli:latest

RUN tdnf install -y powershell && tdnf clean all

WORKDIR /work
COPY update-dns.ps1 /work/update-dns.ps1

ENTRYPOINT ["pwsh", "-NoProfile", "-File", "/work/update-dns.ps1"]
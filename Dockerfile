FROM mcr.microsoft.com/azure-cli:latest

LABEL org.opencontainers.image.title="Azure DDNS"
LABEL org.opencontainers.image.description="Dynamic DNS updater for Azure DNS zones"

# Create app directories
RUN mkdir -p /etc/az-ddns /var/lib/az-ddns

# Copy scripts
COPY az-ddns.sh /usr/local/bin/az-ddns.sh
RUN chmod +x /usr/local/bin/az-ddns.sh

COPY az-ddns-ip.sh /usr/local/bin/az-ddns-ip.sh
RUN chmod +x /usr/local/bin/az-ddns-ip.sh

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

WORKDIR /var/lib/az-ddns

ENV INTERVAL=300
ENV AZ_DNS_ZONE=changeme.example.com
ENV AZ_RECORD_NAME=@
ENV AZ_RECORD_TTL=300
ENV IP_SERVICE_URL=https://api.ipify.org
ENV IP_SERVICE_COMMAND=/usr/local/bin/az-ddns-ip.sh
ENV STATE_FILE=/var/lib/az-ddns/last_ip.txt

# Security: run as non-root azure-cli user (UID 1001 in azure-cli image)
USER 1001

HEALTHCHECK --interval=60s --timeout=15s --start-period=10s --retries=3 \
    CMD pgrep -f az-ddns.sh || exit 1

ENTRYPOINT ["/docker-entrypoint.sh"]

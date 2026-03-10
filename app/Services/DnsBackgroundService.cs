using AzDdns.Models;

namespace AzDdns.Services;

public class DnsBackgroundService : BackgroundService
{
    private readonly ILogger<DnsBackgroundService> _logger;
    private readonly DnsConfigStore _configStore;
    private readonly AzureDnsService _dnsService;
    private readonly DnsCache _cache;
    private readonly IConfiguration _configuration;

    public DnsBackgroundService(
        ILogger<DnsBackgroundService> logger,
        DnsConfigStore configStore,
        AzureDnsService dnsService,
        DnsCache cache,
        IConfiguration configuration)
    {
        _logger = logger;
        _configStore = configStore;
        _dnsService = dnsService;
        _cache = cache;
        _configuration = configuration;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // Initial poll on startup
        await PollAsync(stoppingToken);

        var intervalSeconds = int.TryParse(_configuration["DNS_POLL_INTERVAL"], out var parsed) ? parsed : 1800;

        _logger.LogInformation("DNS background poll interval: {Interval}s", intervalSeconds);

        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(intervalSeconds));
        while (await timer.WaitForNextTickAsync(stoppingToken))
        {
            await PollAsync(stoppingToken);
        }
    }

    private async Task PollAsync(CancellationToken cancellationToken)
    {
        _logger.LogDebug("Starting DNS poll");
        var domains = _configStore.GetManagedDomains();

        var defaultTtl = int.TryParse(_configuration["DNS_TTL"], out var ttl) ? ttl : 3600;

        foreach (var domain in domains)
        {
            if (cancellationToken.IsCancellationRequested)
                break;

            var existing = _cache.Get(domain);
            if (existing is not null && !existing.IsCacheExpired)
            {
                _logger.LogDebug("Cache still valid for {Domain}, skipping poll", domain);
                continue;
            }

            try
            {
                var zone = await _dnsService.FindZoneForDomainAsync(domain);
                if (zone is null)
                {
                    _logger.LogWarning("No matching Azure DNS zone found for {Domain}", domain);
                    UpsertError(domain, existing, defaultTtl, "No matching Azure DNS zone found");
                    continue;
                }

                var ip = await _dnsService.GetARecordAsync(zone.Name, zone.ResourceGroup);
                _cache.Upsert(new DomainEntry
                {
                    Domain = domain,
                    CurrentIp = ip,
                    Ttl = defaultTtl,
                    LastFetched = DateTime.UtcNow,
                    LastUpdated = existing?.LastUpdated,
                    Error = null
                });
                _logger.LogInformation("Polled {Domain}: IP={Ip}", domain, ip ?? "(none)");
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error polling {Domain}", domain);
                UpsertError(domain, existing, defaultTtl, ex.Message);
            }
        }
    }

    private void UpsertError(string domain, DomainEntry? existing, int defaultTtl, string error)
    {
        _cache.Upsert(new DomainEntry
        {
            Domain = domain,
            CurrentIp = existing?.CurrentIp,
            Ttl = existing?.Ttl ?? defaultTtl,
            LastFetched = existing?.LastFetched,
            LastUpdated = existing?.LastUpdated,
            Error = error
        });
    }
}

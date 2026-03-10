using AzDdns.Models;

namespace AzDdns.Services;

/// <summary>
/// Shared service that executes the "update all managed domains to a given IP" logic,
/// used by both the API controller and the Razor Pages dashboard.
/// </summary>
public class DnsUpdateService
{
    private readonly DnsCache _cache;
    private readonly DnsConfigStore _configStore;
    private readonly AzureDnsService _dnsService;
    private readonly IConfiguration _configuration;
    private readonly ILogger<DnsUpdateService> _logger;

    public DnsUpdateService(
        DnsCache cache,
        DnsConfigStore configStore,
        AzureDnsService dnsService,
        IConfiguration configuration,
        ILogger<DnsUpdateService> logger)
    {
        _cache = cache;
        _configStore = configStore;
        _dnsService = dnsService;
        _configuration = configuration;
        _logger = logger;
    }

    /// <summary>
    /// Updates all managed domains to <paramref name="ip"/> and returns per-domain results.
    /// </summary>
    public async Task<(bool Success, string Ip, List<DomainUpdateResult> Results)> UpdateAllAsync(string ip)
    {
        var defaultTtl = int.TryParse(_configuration["DNS_TTL"], out var ttl) ? ttl : 3600;
        var domains = _configStore.GetManagedDomains();
        var results = new List<DomainUpdateResult>();

        foreach (var domain in domains)
        {
            results.Add(await UpdateDomainAsync(domain, ip, defaultTtl));
        }

        var success = results.All(r => r.Action != "error");
        return (success, ip, results);
    }

    private async Task<DomainUpdateResult> UpdateDomainAsync(string domain, string ip, int defaultTtl)
    {
        try
        {
            var zone = await _dnsService.FindZoneForDomainAsync(domain);
            if (zone is null)
            {
                UpdateCacheWithError(domain, "No matching Azure DNS zone found", defaultTtl);
                return new DomainUpdateResult { Domain = domain, Action = "error", Error = "No matching Azure DNS zone found" };
            }

            var existing = _cache.Get(domain);
            if (existing?.CurrentIp == ip)
            {
                return new DomainUpdateResult { Domain = domain, Action = "unchanged", NewIp = ip };
            }

            await _dnsService.SetARecordAsync(zone.Name, zone.ResourceGroup, ip, defaultTtl);

            _cache.Upsert(new DomainEntry
            {
                Domain = domain,
                CurrentIp = ip,
                Ttl = defaultTtl,
                LastFetched = DateTime.UtcNow,
                LastUpdated = DateTime.UtcNow,
                Error = null
            });

            _logger.LogInformation("Updated {Domain} → {Ip}", domain, ip);
            return new DomainUpdateResult { Domain = domain, Action = "updated", NewIp = ip };
        }
        catch (Exception ex)
        {
            _logger.LogError(ex, "Failed to update {Domain}", domain);
            UpdateCacheWithError(domain, ex.Message, defaultTtl);
            return new DomainUpdateResult { Domain = domain, Action = "error", Error = ex.Message };
        }
    }

    private void UpdateCacheWithError(string domain, string error, int defaultTtl)
    {
        var existing = _cache.Get(domain);
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

using Azure.Identity;
using Azure.ResourceManager;
using Azure.ResourceManager.Dns;
using Azure.ResourceManager.Dns.Models;

namespace AzDdns.Services;

public class ZoneInfo
{
    public string Name { get; set; } = string.Empty;
    public string ResourceGroup { get; set; } = string.Empty;
}

public class AzureDnsService
{
    private readonly IConfiguration _configuration;
    private readonly ILogger<AzureDnsService> _logger;
    private ArmClient? _armClient;
    private List<ZoneInfo>? _zonesCache;
    private DateTime _zonesCacheExpiry = DateTime.MinValue;

    public AzureDnsService(IConfiguration configuration, ILogger<AzureDnsService> logger)
    {
        _configuration = configuration;
        _logger = logger;
    }

    private ArmClient GetClient()
    {
        if (_armClient is not null)
            return _armClient;

        var tenantId = _configuration["AZURE_TENANT_ID"];
        var clientId = _configuration["AZURE_CLIENT_ID"];
        var clientSecret = _configuration["AZURE_CLIENT_SECRET"];

        Azure.Core.TokenCredential credential;
        if (!string.IsNullOrWhiteSpace(clientSecret) && !string.IsNullOrWhiteSpace(tenantId) && !string.IsNullOrWhiteSpace(clientId))
        {
            _logger.LogInformation("Using ClientSecretCredential for Azure DNS");
            credential = new ClientSecretCredential(tenantId, clientId, clientSecret);
        }
        else
        {
            _logger.LogInformation("Using DefaultAzureCredential for Azure DNS");
            credential = new DefaultAzureCredential();
        }

        _armClient = new ArmClient(credential);
        return _armClient;
    }

    public async Task<List<ZoneInfo>> ListZonesAsync()
    {
        if (_zonesCache is not null && DateTime.UtcNow < _zonesCacheExpiry)
            return _zonesCache;

        var subscriptionId = _configuration["AZURE_SUBSCRIPTION_ID"];
        if (string.IsNullOrWhiteSpace(subscriptionId))
            throw new InvalidOperationException("AZURE_SUBSCRIPTION_ID is not configured.");

        var client = GetClient();
        var subscription = await client.GetSubscriptionResource(
            new Azure.Core.ResourceIdentifier($"/subscriptions/{subscriptionId}")).GetAsync();

        var zones = new List<ZoneInfo>();
        await foreach (var zone in subscription.Value.GetDnsZonesAsync())
        {
            var rg = zone.Id.ResourceGroupName ?? string.Empty;
            zones.Add(new ZoneInfo { Name = zone.Data.Name, ResourceGroup = rg });
        }

        _zonesCache = zones;
        _zonesCacheExpiry = DateTime.UtcNow.AddMinutes(10);
        _logger.LogInformation("Fetched {Count} DNS zones from Azure", zones.Count);
        return zones;
    }

    public async Task<string?> GetARecordAsync(string zoneName, string resourceGroup)
    {
        var subscriptionId = _configuration["AZURE_SUBSCRIPTION_ID"];
        if (string.IsNullOrWhiteSpace(subscriptionId))
            throw new InvalidOperationException("AZURE_SUBSCRIPTION_ID is not configured.");

        try
        {
            var client = GetClient();
            var zoneId = DnsZoneResource.CreateResourceIdentifier(subscriptionId, resourceGroup, zoneName);
            var zone = client.GetDnsZoneResource(zoneId);
            var recordSetId = DnsARecordResource.CreateResourceIdentifier(subscriptionId, resourceGroup, zoneName, "@");
            var recordSet = client.GetDnsARecordResource(recordSetId);
            var response = await recordSet.GetAsync();
            return response.Value.Data.DnsARecords.FirstOrDefault()?.IPv4Address?.ToString();
        }
        catch (Azure.RequestFailedException ex) when (ex.Status == 404)
        {
            return null;
        }
    }

    public async Task SetARecordAsync(string zoneName, string resourceGroup, string ip, int ttl)
    {
        var subscriptionId = _configuration["AZURE_SUBSCRIPTION_ID"];
        if (string.IsNullOrWhiteSpace(subscriptionId))
            throw new InvalidOperationException("AZURE_SUBSCRIPTION_ID is not configured.");

        var client = GetClient();
        var zoneId = DnsZoneResource.CreateResourceIdentifier(subscriptionId, resourceGroup, zoneName);
        var zone = client.GetDnsZoneResource(zoneId);
        var records = zone.GetDnsARecords();

        var data = new DnsARecordData
        {
            TtlInSeconds = ttl
        };
        data.DnsARecords.Add(new Azure.ResourceManager.Dns.Models.DnsARecordInfo
        {
            IPv4Address = System.Net.IPAddress.Parse(ip)
        });

        await records.CreateOrUpdateAsync(Azure.WaitUntil.Completed, "@", data);
        _logger.LogInformation("Set A record for {Zone} apex to {Ip} (TTL {Ttl})", zoneName, ip, ttl);
    }

    /// <summary>
    /// Finds the best-matching zone for a given domain using longest-suffix match.
    /// </summary>
    public async Task<ZoneInfo?> FindZoneForDomainAsync(string domain)
    {
        var zones = await ListZonesAsync();
        ZoneInfo? best = null;
        foreach (var zone in zones)
        {
            if (domain.Equals(zone.Name, StringComparison.OrdinalIgnoreCase) ||
                domain.EndsWith("." + zone.Name, StringComparison.OrdinalIgnoreCase))
            {
                if (best is null || zone.Name.Length > best.Name.Length)
                    best = zone;
            }
        }
        return best;
    }
}

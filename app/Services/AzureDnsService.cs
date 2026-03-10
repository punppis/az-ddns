using Azure.Identity;
using Azure.ResourceManager;
using Azure.ResourceManager.Dns;
using Azure.ResourceManager.Dns.Models;
using Azure.ResourceManager.Resources;

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
    // Lazily initialised on first use so startup is not blocked
    private ArmClient? _armClient;
    private string? _resolvedSubscriptionId;
    private List<ZoneInfo>? _zonesCache;
    private DateTime _zonesCacheExpiry = DateTime.MinValue;

    public AzureDnsService(IConfiguration configuration, ILogger<AzureDnsService> logger)
    {
        _configuration = configuration;
        _logger = logger;
    }

    /// <summary>
    /// Returns the ARM client, always using <see cref="DefaultAzureCredential"/>.
    /// On an Azure VM with a managed identity assigned this requires no additional
    /// configuration. When running locally, <c>az login</c> or env vars are used.
    /// </summary>
    private ArmClient GetClient()
    {
        if (_armClient is not null)
            return _armClient;

        _logger.LogInformation("Using DefaultAzureCredential for Azure DNS");
        _armClient = new ArmClient(new DefaultAzureCredential());
        return _armClient;
    }

    /// <summary>
    /// Returns the subscription ID to use. If <c>AZURE_SUBSCRIPTION_ID</c> is set
    /// in configuration it is used as-is. Otherwise the first subscription returned
    /// by the ARM API is used (convenient on a single-subscription managed identity).
    /// </summary>
    private async Task<string> GetSubscriptionIdAsync()
    {
        if (_resolvedSubscriptionId is not null)
            return _resolvedSubscriptionId;

        var configured = _configuration["AZURE_SUBSCRIPTION_ID"];
        if (!string.IsNullOrWhiteSpace(configured))
        {
            _resolvedSubscriptionId = configured;
            return _resolvedSubscriptionId;
        }

        _logger.LogInformation("AZURE_SUBSCRIPTION_ID not set — auto-discovering from ARM...");
        var client = GetClient();
        await foreach (var sub in client.GetSubscriptions().GetAllAsync())
        {
            _resolvedSubscriptionId = sub.Data.SubscriptionId;
            _logger.LogInformation("Auto-discovered subscription: {Name} ({Id})",
                sub.Data.DisplayName, _resolvedSubscriptionId);
            break;
        }

        if (_resolvedSubscriptionId is null)
            throw new InvalidOperationException(
                "Could not determine Azure subscription. " +
                "Set AZURE_SUBSCRIPTION_ID or ensure the managed identity has at least one subscription visible.");

        return _resolvedSubscriptionId;
    }

    public async Task<List<ZoneInfo>> ListZonesAsync()
    {
        if (_zonesCache is not null && DateTime.UtcNow < _zonesCacheExpiry)
            return _zonesCache;

        var subscriptionId = await GetSubscriptionIdAsync();
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
        var subscriptionId = await GetSubscriptionIdAsync();
        try
        {
            var client = GetClient();
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
        var subscriptionId = await GetSubscriptionIdAsync();
        var client = GetClient();
        var zoneId = DnsZoneResource.CreateResourceIdentifier(subscriptionId, resourceGroup, zoneName);
        var zone = client.GetDnsZoneResource(zoneId);
        var records = zone.GetDnsARecords();

        var data = new DnsARecordData { TtlInSeconds = ttl };
        data.DnsARecords.Add(new DnsARecordInfo { IPv4Address = System.Net.IPAddress.Parse(ip) });

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

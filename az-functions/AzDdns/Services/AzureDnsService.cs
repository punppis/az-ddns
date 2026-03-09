using Azure;
using Azure.Core;
using Azure.Identity;
using Azure.ResourceManager;
using Azure.ResourceManager.Authorization;
using Azure.ResourceManager.Authorization.Models;
using Azure.ResourceManager.Dns;
using Azure.ResourceManager.Dns.Models;
using AzDdns.Models;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace AzDdns.Services;

/// <summary>
/// Wraps the Azure Resource Manager DNS SDK.
/// Authenticates with the service principal credentials from env vars
/// (or managed identity when no client secret is provided).
/// </summary>
public sealed class AzureDnsService
{
    // Well-known RBAC role definition IDs
    // DNS Zone Contributor: befefa01-2a29-4197-83a8-272ff33ce314
    private const string DnsZoneContributorRoleId = "befefa01-2a29-4197-83a8-272ff33ce314";

    private readonly ILogger<AzureDnsService> _logger;
    private readonly int _defaultTtl;
    private readonly int _defaultMxPreference;
    private readonly ArmClient _armClient;
    private readonly string _subscriptionId;

    // Cached zone list: refreshed lazily or on demand
    private List<(string ZoneName, string ResourceGroup, string SubscriptionId)>? _zoneCache;
    private DateTimeOffset _zoneCacheExpiry = DateTimeOffset.MinValue;

    public AzureDnsService(IConfiguration cfg, ILogger<AzureDnsService> logger)
    {
        _logger = logger;
        _defaultTtl = cfg.GetValue<int?>("DNS_TTL") ?? 3600;
        _defaultMxPreference = cfg.GetValue<int?>("DNS_MX_PREFERENCE") ?? 10;

        var tenantId = Required(cfg, "AZURE_TENANT_ID");
        var clientId = Required(cfg, "AZURE_CLIENT_ID");
        _subscriptionId = Required(cfg, "AZURE_SUBSCRIPTION_ID");

        var clientSecret = cfg["AZURE_CLIENT_SECRET"];

        TokenCredential cred;
        if (!string.IsNullOrWhiteSpace(clientSecret))
        {
            cred = new ClientSecretCredential(tenantId, clientId, clientSecret);
            _logger.LogInformation("Azure auth: using ClientSecretCredential (service principal).");
        }
        else
        {
            // Managed identity / workload identity fallback
            cred = new DefaultAzureCredential(new DefaultAzureCredentialOptions
            {
                ManagedIdentityClientId = clientId,
                TenantId = tenantId
            });
            _logger.LogInformation("Azure auth: using DefaultAzureCredential (managed identity / workload identity).");
        }

        _armClient = new ArmClient(cred);
    }

    private static string Required(IConfiguration cfg, string key)
    {
        var val = cfg[key];
        if (string.IsNullOrWhiteSpace(val))
            throw new InvalidOperationException($"Missing required configuration value: {key}");
        return val;
    }

    // -------------------------------------------------------------------------
    // Zone discovery
    // -------------------------------------------------------------------------

    /// <summary>Lists all DNS zones accessible to this credential.</summary>
    public async Task<List<(string ZoneName, string ResourceGroup, string SubscriptionId)>> ListZonesAsync()
    {
        if (_zoneCache is not null && DateTimeOffset.UtcNow < _zoneCacheExpiry)
            return _zoneCache;

        _logger.LogInformation("Fetching DNS zones from subscription {Sub}...", _subscriptionId);
        var sub = _armClient.GetSubscriptionResource(
            SubscriptionResource.CreateResourceIdentifier(_subscriptionId));

        var zones = new List<(string, string, string)>();
        await foreach (var zone in sub.GetDnsZonesAsync())
        {
            var rg = zone.Id.ResourceGroupName!;
            zones.Add((zone.Data.Name, rg, _subscriptionId));
        }

        _logger.LogInformation("Found {Count} DNS zone(s).", zones.Count);
        _zoneCache = zones;
        _zoneCacheExpiry = DateTimeOffset.UtcNow.AddMinutes(10);
        return zones;
    }

    /// <summary>
    /// Returns the best-matching zone for <paramref name="domain"/> (longest-suffix match).
    /// Returns null if no match found.
    /// </summary>
    public async Task<(string ZoneName, string ResourceGroup)?> FindZoneForDomainAsync(string domain)
    {
        var zones = await ListZonesAsync();
        var domainLower = domain.ToLowerInvariant().TrimEnd('.');

        string? bestZone = null;
        string? bestRg = null;
        int bestLen = 0;

        foreach (var (zoneName, rg, _) in zones)
        {
            var zl = zoneName.ToLowerInvariant().TrimEnd('.');
            if (domainLower == zl || domainLower.EndsWith("." + zl, StringComparison.Ordinal))
            {
                if (zl.Length > bestLen)
                {
                    bestLen = zl.Length;
                    bestZone = zoneName;
                    bestRg = rg;
                }
            }
        }

        if (bestZone is null)
        {
            _logger.LogError("No Azure DNS zone found for domain '{Domain}'.", domain);
            return null;
        }

        return (bestZone, bestRg!);
    }

    // -------------------------------------------------------------------------
    // Record helpers
    // -------------------------------------------------------------------------

    private static string RelativeName(string domain, string zone)
    {
        var dl = domain.ToLowerInvariant().TrimEnd('.');
        var zl = zone.ToLowerInvariant().TrimEnd('.');
        if (dl == zl) return "@";
        if (dl.EndsWith("." + zl, StringComparison.Ordinal))
            return dl[..^(zl.Length + 1)]; // strip trailing ".zone"
        return dl;
    }

    /// <summary>Replaces {{IP}} and {{DOMAIN}} placeholders.</summary>
    public static string ResolvePlaceholders(string template, string ip, string domain)
        => template.Replace("{{IP}}", ip).Replace("{{DOMAIN}}", domain);

    // -------------------------------------------------------------------------
    // List live records
    // -------------------------------------------------------------------------

    /// <summary>Returns live A, CNAME, and MX records for a zone.</summary>
    public async Task<List<LiveRecord>> ListRecordsAsync(string zoneName, string resourceGroup)
    {
        var results = new List<LiveRecord>();
        var zoneRes = _armClient.GetDnsZoneResource(
            DnsZoneResource.CreateResourceIdentifier(_subscriptionId, resourceGroup, zoneName));

        await foreach (var rs in zoneRes.GetAllRecordDataAsync())
        {
            var typeName = rs.ResourceType.Type.Split('/').Last().ToUpperInvariant();
            if (typeName is not ("A" or "CNAME" or "MX")) continue;

            var value = typeName switch
            {
                "A" => string.Join(", ", rs.DnsARecords.Select(r => r.IPv4Address.ToString())),
                "CNAME" => rs.DnsCnameRecord?.Cname ?? "",
                "MX" => string.Join(", ", rs.DnsMxRecords.Select(r =>
                    $"{r.Preference} {r.Exchange?.ToString() ?? ""}")),
                _ => ""
            };

            results.Add(new LiveRecord
            {
                Name = rs.Name ?? "@",
                Type = typeName,
                Value = value.TrimEnd('.'),
                Ttl = rs.TtlInSeconds ?? _defaultTtl
            });
        }

        return results;
    }

    // -------------------------------------------------------------------------
    // Update records
    // -------------------------------------------------------------------------

    /// <summary>
    /// Updates all DNS records for <paramref name="domain"/> according to
    /// <paramref name="domainCfg"/>. Returns per-record results.
    /// </summary>
    public async Task<DomainResult> UpdateDomainAsync(
        string domain,
        DomainConfig domainCfg,
        string ip,
        int ttl,
        int mxPreference)
    {
        var result = new DomainResult { Domain = domain };

        var zoneMatch = await FindZoneForDomainAsync(domain);
        if (zoneMatch is null)
        {
            result.Error = $"No Azure DNS zone found for domain '{domain}'.";
            _logger.LogError("No zone found for domain '{Domain}'.", domain);
            return result;
        }

        var (zoneName, rg) = zoneMatch.Value;
        result.Zone = zoneName;
        result.ResourceGroup = rg;

        var zoneRes = _armClient.GetDnsZoneResource(
            DnsZoneResource.CreateResourceIdentifier(_subscriptionId, rg, zoneName));

        foreach (var (rtype, recordSet) in domainCfg)
        {
            var rt = rtype.ToUpperInvariant();
            foreach (var (recordName, template) in recordSet)
            {
                var desired = ResolvePlaceholders(template, ip, domain);
                // For relative name within the zone
                var relName = recordName == "@"
                    ? RelativeName(domain, zoneName)
                    : (recordName == "@" ? "@" : $"{recordName}");

                // Fully resolve: if record name contains the domain, compute relative
                if (relName.EndsWith("." + zoneName, StringComparison.OrdinalIgnoreCase))
                    relName = relName[..^(zoneName.Length + 1)];
                if (string.IsNullOrEmpty(relName)) relName = "@";

                var rsr = new RecordSetResult { Name = relName == "@" ? domain : $"{relName}.{domain}", Type = rt, Value = desired };

                try
                {
                    await UpsertRecordAsync(zoneRes, rt, relName, desired, ttl, mxPreference, rsr);
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Error updating {Type} {Name} in {Zone}.", rt, relName, zoneName);
                    rsr.Action = "error";
                    rsr.Error = ex.Message;
                }

                result.Records.Add(rsr);
            }
        }

        return result;
    }

    private async Task UpsertRecordAsync(
        DnsZoneResource zone,
        string rt,
        string relName,
        string desired,
        int ttl,
        int mxPreference,
        RecordSetResult rsr)
    {
        switch (rt)
        {
            case "A":
                await UpsertARecordAsync(zone, relName, desired, ttl, rsr);
                break;
            case "CNAME":
                await UpsertCnameRecordAsync(zone, relName, desired, ttl, rsr);
                break;
            case "MX":
                await UpsertMxRecordAsync(zone, relName, desired, ttl, mxPreference, rsr);
                break;
            default:
                throw new ArgumentException($"Unsupported record type: {rt}");
        }
    }

    private async Task UpsertARecordAsync(DnsZoneResource zone, string name, string ip, int ttl, RecordSetResult rsr)
    {
        var collection = zone.GetDnsARecordSets();
        DnsARecordSetData data;

        // Check existing value
        try
        {
            var existing = await collection.GetAsync(name);
            var existingIp = existing.Value.Data.DnsARecords.FirstOrDefault()?.IPv4Address.ToString();
            if (existingIp == ip)
            {
                _logger.LogInformation("A {Name} already points to {Ip}; no update needed.", name, ip);
                rsr.Action = "unchanged";
                rsr.Ttl = existing.Value.Data.TtlInSeconds ?? ttl;
                return;
            }
            _logger.LogInformation("Updating A {Name}: {Old} -> {New}", name, existingIp, ip);
        }
        catch (RequestFailedException ex) when (ex.Status == 404)
        {
            _logger.LogInformation("Creating A record {Name} -> {Ip}", name, ip);
        }

        data = new DnsARecordSetData { TtlInSeconds = ttl };
        data.DnsARecords.Add(new DnsARecordInfo { IPv4Address = System.Net.IPAddress.Parse(ip) });
        await collection.CreateOrUpdateAsync(WaitUntil.Completed, name, data);

        rsr.Action = "updated";
        rsr.Ttl = ttl;
        _logger.LogInformation("  SET A {Name} -> {Ip} (TTL {Ttl})", name, ip, ttl);
    }

    private async Task UpsertCnameRecordAsync(DnsZoneResource zone, string name, string target, int ttl, RecordSetResult rsr)
    {
        var collection = zone.GetDnsCnameRecordSets();

        try
        {
            var existing = await collection.GetAsync(name);
            var existingTarget = existing.Value.Data.DnsCnameRecord?.Cname?.TrimEnd('.');
            if (existingTarget == target.TrimEnd('.'))
            {
                _logger.LogInformation("CNAME {Name} already points to {Target}; no update needed.", name, target);
                rsr.Action = "unchanged";
                rsr.Ttl = existing.Value.Data.TtlInSeconds ?? ttl;
                return;
            }
            _logger.LogInformation("Updating CNAME {Name}: {Old} -> {New}", name, existingTarget, target);
        }
        catch (RequestFailedException ex) when (ex.Status == 404)
        {
            _logger.LogInformation("Creating CNAME record {Name} -> {Target}", name, target);
        }

        var data = new DnsCnameRecordSetData { TtlInSeconds = ttl, DnsCnameRecord = new DnsCnameRecordInfo { Cname = target } };
        await collection.CreateOrUpdateAsync(WaitUntil.Completed, name, data);

        rsr.Action = "updated";
        rsr.Ttl = ttl;
        _logger.LogInformation("  SET CNAME {Name} -> {Target} (TTL {Ttl})", name, target, ttl);
    }

    private async Task UpsertMxRecordAsync(DnsZoneResource zone, string name, string exchange, int ttl, int preference, RecordSetResult rsr)
    {
        var collection = zone.GetDnsMxRecordSets();

        try
        {
            var existing = await collection.GetAsync(name);
            var existingExch = existing.Value.Data.DnsMxRecords.FirstOrDefault()?.Exchange?.ToString()?.TrimEnd('.');
            if (existingExch == exchange.TrimEnd('.'))
            {
                _logger.LogInformation("MX {Name} already points to {Exchange}; no update needed.", name, exchange);
                rsr.Action = "unchanged";
                rsr.Ttl = existing.Value.Data.TtlInSeconds ?? ttl;
                return;
            }
            _logger.LogInformation("Updating MX {Name}: {Old} -> {New}", name, existingExch, exchange);
        }
        catch (RequestFailedException ex) when (ex.Status == 404)
        {
            _logger.LogInformation("Creating MX record {Name} -> {Exchange}", name, exchange);
        }

        var data = new DnsMxRecordSetData { TtlInSeconds = ttl };
        data.DnsMxRecords.Add(new DnsMxRecordInfo { Preference = preference, Exchange = new Azure.Core.DnsName(exchange) });
        await collection.CreateOrUpdateAsync(WaitUntil.Completed, name, data);

        rsr.Action = "updated";
        rsr.Ttl = ttl;
        _logger.LogInformation("  SET MX {Name} -> {Exchange} pref={Pref} (TTL {Ttl})", name, exchange, preference, ttl);
    }

    // -------------------------------------------------------------------------
    // RBAC: ensure the service principal has DNS Zone Contributor
    // -------------------------------------------------------------------------

    /// <summary>
    /// Checks whether the configured service principal has the
    /// <c>DNS Zone Contributor</c> role on the subscription.
    /// Logs a warning if it is missing but does NOT throw — the caller
    /// should decide whether to continue or abort.
    /// </summary>
    public async Task VerifyRolesAsync()
    {
        try
        {
            var sub = _armClient.GetSubscriptionResource(
                SubscriptionResource.CreateResourceIdentifier(_subscriptionId));

            var roleDefId = new ResourceIdentifier(
                $"/subscriptions/{_subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/{DnsZoneContributorRoleId}");

            bool found = false;
            await foreach (var assignment in sub.GetRoleAssignmentsAsync())
            {
                if (assignment.Data.RoleDefinitionId == roleDefId)
                {
                    found = true;
                    break;
                }
            }

            if (found)
                _logger.LogInformation("RBAC check: DNS Zone Contributor role is assigned on the subscription.");
            else
                _logger.LogWarning(
                    "RBAC check: DNS Zone Contributor role NOT found on subscription {Sub}. " +
                    "Run init.sh to assign it or ensure the principal has sufficient permissions.",
                    _subscriptionId);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "RBAC check could not be completed; skipping.");
        }
    }
}

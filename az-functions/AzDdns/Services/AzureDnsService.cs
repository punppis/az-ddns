using Azure;
using Azure.Core;
using Azure.Identity;
using Azure.ResourceManager;
using Azure.ResourceManager.Authorization;
using Azure.ResourceManager.Dns;
using Azure.ResourceManager.Dns.Models;
using Azure.ResourceManager.Resources;
using AzDdns.Models;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Threading.Tasks;

namespace AzDdns.Services;

/// <summary>
/// Wraps the Azure Resource Manager DNS SDK.
/// Authenticates with the service principal credentials from env vars
/// (or managed identity when no client secret is provided).
/// </summary>
public sealed class AzureDnsService
{
    // DNS Zone Contributor role definition ID (well-known, stable across all tenants)
    private const string DnsZoneContributorRoleId = "befefa01-2a29-4197-83a8-272ff33ce314";

    private readonly ILogger<AzureDnsService> _logger;
    private readonly int _defaultTtl;
    private readonly int _defaultMxPreference;
    private readonly ArmClient _armClient;
    private readonly string _subscriptionId;

    // Zone list cache (refreshed every 10 minutes)
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

    /// <summary>Lists all DNS zones accessible to this credential (cached 10 min).</summary>
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
            zones.Add((zone.Data.Name, zone.Id.ResourceGroupName!, _subscriptionId));
        }

        _logger.LogInformation("Found {Count} DNS zone(s).", zones.Count);
        _zoneCache = zones;
        _zoneCacheExpiry = DateTimeOffset.UtcNow.AddMinutes(10);
        return zones;
    }

    /// <summary>
    /// Returns the best-matching zone for <paramref name="domain"/> (longest-suffix match),
    /// or null with a logged error if no match is found.
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
    // Helpers
    // -------------------------------------------------------------------------

    private static string RelativeName(string domain, string zone)
    {
        var dl = domain.ToLowerInvariant().TrimEnd('.');
        var zl = zone.ToLowerInvariant().TrimEnd('.');
        if (dl == zl) return "@";
        if (dl.EndsWith("." + zl, StringComparison.Ordinal))
            return dl[..^(zl.Length + 1)];
        return dl;
    }

    /// <summary>Replaces {{IP}} and {{DOMAIN}} placeholders in a record-value template.</summary>
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
            string value;

            switch (typeName)
            {
                case "A":
                    value = string.Join(", ", rs.DnsARecords.Select(r => r.IPv4Address?.ToString() ?? ""));
                    break;
                case "CNAME":
                    value = (rs.Cname ?? "").TrimEnd('.');
                    break;
                case "MX":
                    value = string.Join(", ", rs.DnsMXRecords.Select(r =>
                        $"{r.Preference} {(r.Exchange ?? "").TrimEnd('.')}"));
                    break;
                default:
                    continue;
            }

            results.Add(new LiveRecord
            {
                Name = rs.Name ?? "@",
                Type = typeName,
                Value = value,
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
                var relName = recordName == "@" ? RelativeName(domain, zoneName) : recordName;
                if (string.IsNullOrEmpty(relName)) relName = "@";

                var rsr = new RecordSetResult
                {
                    Name = relName == "@" ? domain : $"{relName}.{domain}",
                    Type = rt,
                    Value = desired
                };

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

    private Task UpsertRecordAsync(
        DnsZoneResource zone, string rt, string relName, string desired,
        int ttl, int mxPreference, RecordSetResult rsr) => rt switch
    {
        "A"     => UpsertARecordAsync(zone, relName, desired, ttl, rsr),
        "CNAME" => UpsertCnameRecordAsync(zone, relName, desired, ttl, rsr),
        "MX"    => UpsertMxRecordAsync(zone, relName, desired, ttl, mxPreference, rsr),
        _       => throw new ArgumentException($"Unsupported record type: {rt}")
    };

    private async Task UpsertARecordAsync(
        DnsZoneResource zone, string name, string ip, int ttl, RecordSetResult rsr)
    {
        var collection = zone.GetDnsARecords();

        try
        {
            var existing = await collection.GetAsync(name);
            var existingIp = existing.Value.Data.DnsARecords.FirstOrDefault()?.IPv4Address?.ToString();
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

        var data = new DnsARecordData { TtlInSeconds = ttl };
        data.DnsARecords.Add(new DnsARecordInfo { IPv4Address = IPAddress.Parse(ip) });
        await collection.CreateOrUpdateAsync(WaitUntil.Completed, name, data);

        rsr.Action = "updated";
        rsr.Ttl = ttl;
        _logger.LogInformation("  SET A {Name} -> {Ip} (TTL {Ttl})", name, ip, ttl);
    }

    private async Task UpsertCnameRecordAsync(
        DnsZoneResource zone, string name, string target, int ttl, RecordSetResult rsr)
    {
        var collection = zone.GetDnsCnameRecords();

        try
        {
            var existing = await collection.GetAsync(name);
            var existingTarget = (existing.Value.Data.Cname ?? "").TrimEnd('.');
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

        var data = new DnsCnameRecordData { Cname = target, TtlInSeconds = ttl };
        await collection.CreateOrUpdateAsync(WaitUntil.Completed, name, data);

        rsr.Action = "updated";
        rsr.Ttl = ttl;
        _logger.LogInformation("  SET CNAME {Name} -> {Target} (TTL {Ttl})", name, target, ttl);
    }

    private async Task UpsertMxRecordAsync(
        DnsZoneResource zone, string name, string exchange, int ttl, int preference, RecordSetResult rsr)
    {
        var collection = zone.GetDnsMXRecords();

        try
        {
            var existing = await collection.GetAsync(name);
            var existingExch = (existing.Value.Data.DnsMXRecords.FirstOrDefault()?.Exchange ?? "").TrimEnd('.');
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

        var data = new DnsMXRecordData { TtlInSeconds = ttl };
        data.DnsMXRecords.Add(new DnsMXRecordInfo { Exchange = exchange, Preference = preference });
        await collection.CreateOrUpdateAsync(WaitUntil.Completed, name, data);

        rsr.Action = "updated";
        rsr.Ttl = ttl;
        _logger.LogInformation("  SET MX {Name} -> {Exchange} pref={Pref} (TTL {Ttl})", name, exchange, preference, ttl);
    }

    // -------------------------------------------------------------------------
    // RBAC: verify the service principal has DNS Zone Contributor
    // -------------------------------------------------------------------------

    /// <summary>
    /// Checks whether any role assignment on the subscription matches
    /// <c>DNS Zone Contributor</c>. Logs a warning when missing but does NOT throw.
    /// </summary>
    public async Task VerifyRolesAsync()
    {
        try
        {
            var scope = new ResourceIdentifier($"/subscriptions/{_subscriptionId}");
            var roleDefId = new ResourceIdentifier(
                $"/subscriptions/{_subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/{DnsZoneContributorRoleId}");

            bool found = false;
            var assignments = _armClient.GetRoleAssignments(scope);
            await foreach (var assignment in assignments.GetAllAsync())
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
                    "Run init.py to assign it, or ensure the principal has sufficient permissions.",
                    _subscriptionId);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "RBAC check could not be completed; skipping.");
        }
    }
}


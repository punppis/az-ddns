using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace AzDdns.Models;

/// <summary>
/// Per-record-type map: key = record name ("@", "*", "mail", etc.), value = desired value or "{{IP}}"/"{{DOMAIN}}" placeholder.
/// </summary>
public sealed class DnsRecordSet : Dictionary<string, string> { }

/// <summary>
/// One domain's DNS configuration.
/// Key = record type (A, CNAME, MX). Value = name-to-value map.
/// </summary>
public sealed class DomainConfig : Dictionary<string, DnsRecordSet> { }

/// <summary>Request body for POST /api/update.</summary>
public sealed class UpdateRequest
{
    /// <summary>
    /// Override IP to use instead of the caller's detected IP.
    /// Leave null/empty to use the caller's public IP automatically.
    /// </summary>
    [JsonPropertyName("ip")]
    public string? Ip { get; set; }

    /// <summary>
    /// DNS records to update.
    /// Key = domain name (e.g. "office.example.com").
    /// Value = record-type map (A, CNAME, MX) with name→value pairs.
    /// Supports {{IP}} and {{DOMAIN}} placeholders.
    /// </summary>
    [JsonPropertyName("records")]
    public Dictionary<string, DomainConfig> Records { get; set; } = new();

    /// <summary>
    /// Optional TTL in seconds (overrides server default).
    /// </summary>
    [JsonPropertyName("ttl")]
    public int? Ttl { get; set; }

    /// <summary>
    /// MX preference value (overrides server default).
    /// </summary>
    [JsonPropertyName("mxPreference")]
    public int? MxPreference { get; set; }
}

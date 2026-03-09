using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace AzDdns.Models;

public sealed class LiveRecord
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("value")]
    public string Value { get; set; } = "";

    [JsonPropertyName("ttl")]
    public long Ttl { get; set; }
}

public sealed class ZoneInfo
{
    [JsonPropertyName("domain")]
    public string Domain { get; set; } = "";

    [JsonPropertyName("zone")]
    public string Zone { get; set; } = "";

    [JsonPropertyName("resourceGroup")]
    public string ResourceGroup { get; set; } = "";

    [JsonPropertyName("records")]
    public List<LiveRecord> Records { get; set; } = new();

    [JsonPropertyName("error")]
    public string? Error { get; set; }
}

public sealed class ListResponse
{
    [JsonPropertyName("success")]
    public bool Success { get; set; }

    [JsonPropertyName("zones")]
    public List<ZoneInfo> Zones { get; set; } = new();

    [JsonPropertyName("summary")]
    public string Summary { get; set; } = "";
}

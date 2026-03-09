using System.Collections.Generic;
using System.Text.Json.Serialization;

namespace AzDdns.Models;

public sealed class RecordSetResult
{
    [JsonPropertyName("name")]
    public string Name { get; set; } = "";

    [JsonPropertyName("type")]
    public string Type { get; set; } = "";

    [JsonPropertyName("value")]
    public string Value { get; set; } = "";

    [JsonPropertyName("ttl")]
    public long Ttl { get; set; }

    [JsonPropertyName("action")]
    public string Action { get; set; } = "unchanged"; // "updated", "unchanged", "error"

    [JsonPropertyName("error")]
    public string? Error { get; set; }
}

public sealed class DomainResult
{
    [JsonPropertyName("domain")]
    public string Domain { get; set; } = "";

    [JsonPropertyName("zone")]
    public string Zone { get; set; } = "";

    [JsonPropertyName("resourceGroup")]
    public string ResourceGroup { get; set; } = "";

    [JsonPropertyName("records")]
    public List<RecordSetResult> Records { get; set; } = new();

    [JsonPropertyName("error")]
    public string? Error { get; set; }
}

public sealed class UpdateResponse
{
    [JsonPropertyName("success")]
    public bool Success { get; set; }

    [JsonPropertyName("detectedIp")]
    public string DetectedIp { get; set; } = "";

    [JsonPropertyName("domains")]
    public List<DomainResult> Domains { get; set; } = new();

    [JsonPropertyName("updatedCount")]
    public int UpdatedCount { get; set; }

    [JsonPropertyName("unchangedCount")]
    public int UnchangedCount { get; set; }

    [JsonPropertyName("errorCount")]
    public int ErrorCount { get; set; }

    [JsonPropertyName("summary")]
    public string Summary { get; set; } = "";
}

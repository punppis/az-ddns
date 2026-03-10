using System.Text.Json;
using System.Text.Json.Serialization;

namespace AzDdns.Models;

public class DnsConfig
{
    [JsonPropertyName("lastUpdate")]
    public string LastUpdate { get; set; } = "";

    [JsonPropertyName("domains")]
    public Dictionary<string, DomainConfig> Domains { get; set; } = new();

    /// <summary>Preserve the legacy Python-updater "state" field if present.</summary>
    [JsonPropertyName("state")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public JsonElement? State { get; set; }
}

public class DomainConfig
{
    [JsonPropertyName("A")]
    [JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)]
    public Dictionary<string, string>? A { get; set; }
}

namespace AzDdns.Models;

public sealed class DomainState
{
    public string Domain { get; set; } = string.Empty;
    public string? Ip { get; set; }
    public int Ttl { get; set; }
    public string? ZoneName { get; set; }
    public string? ResourceGroup { get; set; }
    public DateTimeOffset LastRefreshedUtc { get; set; }
    public DateTimeOffset NextRefreshUtc { get; set; }
    public DateTimeOffset? LastUpdatedUtc { get; set; }
}

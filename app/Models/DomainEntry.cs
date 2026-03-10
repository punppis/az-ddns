namespace AzDdns.Models;

public class DomainEntry
{
    public string Domain { get; set; } = string.Empty;
    public string? CurrentIp { get; set; }
    public int Ttl { get; set; }
    public DateTime? LastFetched { get; set; }
    public DateTime? LastUpdated { get; set; }
    public string? Error { get; set; }

    public bool IsCacheExpired =>
        LastFetched is null ||
        Ttl <= 0 ||
        DateTime.UtcNow > LastFetched.Value.AddSeconds(Ttl / 2.0);
}

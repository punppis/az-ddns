namespace AzDdns.Models;

public class ListResponse
{
    public bool Success { get; set; }
    public List<DomainInfo> Domains { get; set; } = new();
}

public class DomainInfo
{
    public string Domain { get; set; } = string.Empty;
    public string? CurrentIp { get; set; }
    public int Ttl { get; set; }
    public DateTime? LastFetched { get; set; }
    public DateTime? LastUpdated { get; set; }
    public string? Error { get; set; }
}

namespace AzDdns.Models;

public class DnsConfig
{
    public Dictionary<string, DomainConfig> Domains { get; set; } = new();
}

public class DomainConfig
{
    public Dictionary<string, string> A { get; set; } = new();
}

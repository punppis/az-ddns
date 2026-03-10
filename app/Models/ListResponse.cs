namespace AzDdns.Models;

public sealed class ListResponse
{
    public List<DomainState> Domains { get; set; } = new();
}

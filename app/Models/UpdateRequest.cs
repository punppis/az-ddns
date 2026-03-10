namespace AzDdns.Models;

public sealed class UpdateRequest
{
    public string? Ip { get; set; }
    public List<string>? Domains { get; set; }
}

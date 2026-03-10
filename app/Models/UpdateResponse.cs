namespace AzDdns.Models;

public sealed class UpdateResponse
{
    public string DetectedIp { get; set; } = string.Empty;
    public List<DomainUpdateResult> Results { get; set; } = new();
}

public sealed class DomainUpdateResult
{
    public string Domain { get; set; } = string.Empty;
    public string? PreviousIp { get; set; }
    public string? NewIp { get; set; }
    public string Status { get; set; } = string.Empty;
    public string? Error { get; set; }
    public string? ZoneName { get; set; }
    public string? ResourceGroup { get; set; }
}

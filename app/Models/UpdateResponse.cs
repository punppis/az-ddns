namespace AzDdns.Models;

public class UpdateResponse
{
    public bool Success { get; set; }
    public string? DetectedIp { get; set; }
    public List<DomainUpdateResult> Results { get; set; } = new();
}

public class DomainUpdateResult
{
    public string Domain { get; set; } = string.Empty;
    public string Action { get; set; } = string.Empty; // "updated" | "unchanged" | "error"
    public string? NewIp { get; set; }
    public string? Error { get; set; }
}

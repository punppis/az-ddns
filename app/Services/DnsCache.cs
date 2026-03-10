using System.Collections.Concurrent;
using System.Text.Json;
using AzDdns.Models;
using Microsoft.Extensions.Logging;

namespace AzDdns.Services;

public class DnsCache
{
    private readonly ConcurrentDictionary<string, DomainEntry> _cache = new(StringComparer.OrdinalIgnoreCase);
    private readonly string _statePath;
    private readonly ILogger<DnsCache> _logger;

    public DnsCache(IConfiguration configuration, ILogger<DnsCache> logger)
    {
        _logger = logger;
        var isContainer = Environment.GetEnvironmentVariable("DOTNET_RUNNING_IN_CONTAINER") == "true";
        var dataPath = configuration["DATA_PATH"] ?? (isContainer ? "/data" : ".");
        _statePath = Path.Combine(dataPath, "state.json");
        LoadFromDisk();
    }

    public IEnumerable<DomainEntry> GetAll() => _cache.Values;

    public DomainEntry? Get(string domain) =>
        _cache.TryGetValue(domain, out var entry) ? entry : null;

    public void Upsert(DomainEntry entry)
    {
        _cache[entry.Domain] = entry;
        PersistToDisk();
    }

    public void Remove(string domain)
    {
        _cache.TryRemove(domain, out _);
        PersistToDisk();
    }

    private void LoadFromDisk()
    {
        if (!File.Exists(_statePath))
            return;

        try
        {
            var json = File.ReadAllText(_statePath);
            var entries = JsonSerializer.Deserialize<List<DomainEntry>>(json,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });

            if (entries is not null)
            {
                foreach (var entry in entries)
                    _cache[entry.Domain] = entry;

                _logger.LogInformation("Loaded {Count} domain state entries from {Path}", entries.Count, _statePath);
            }
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to load state from {Path}", _statePath);
        }
    }

    private void PersistToDisk()
    {
        try
        {
            var dir = Path.GetDirectoryName(_statePath);
            if (!string.IsNullOrEmpty(dir))
                Directory.CreateDirectory(dir);

            var json = JsonSerializer.Serialize(_cache.Values.ToList(),
                new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(_statePath, json);
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to persist state to {Path}", _statePath);
        }
    }
}

using System.Text.Json;
using AzDdns.Models;

namespace AzDdns.Services;

public class DnsConfigStore
{
    private readonly string _configPath;
    private readonly ILogger<DnsConfigStore> _logger;
    private readonly SemaphoreSlim _lock = new(1, 1);

    public DnsConfigStore(IConfiguration configuration, ILogger<DnsConfigStore> logger)
    {
        _logger = logger;
        var isContainer = Environment.GetEnvironmentVariable("DOTNET_RUNNING_IN_CONTAINER") == "true";
        var dataPath = configuration["DATA_PATH"] ?? (isContainer ? "/data" : ".");
        _configPath = Path.Combine(dataPath, "dns.json");
    }

    public List<string> GetManagedDomains()
    {
        var config = ReadConfig();
        return config.Domains.Keys.ToList();
    }

    public async Task AddDomainAsync(string domain)
    {
        await _lock.WaitAsync();
        try
        {
            var config = ReadConfig();
            if (!config.Domains.ContainsKey(domain))
            {
                config.Domains[domain] = new DomainConfig { A = new Dictionary<string, string> { ["@"] = "{{IP}}" } };
                WriteConfig(config);
                _logger.LogInformation("Added domain {Domain} to dns.json", domain);
            }
        }
        finally
        {
            _lock.Release();
        }
    }

    public async Task RemoveDomainAsync(string domain)
    {
        await _lock.WaitAsync();
        try
        {
            var config = ReadConfig();
            if (config.Domains.Remove(domain))
            {
                WriteConfig(config);
                _logger.LogInformation("Removed domain {Domain} from dns.json", domain);
            }
        }
        finally
        {
            _lock.Release();
        }
    }

    private DnsConfig ReadConfig()
    {
        if (!File.Exists(_configPath))
            return new DnsConfig();

        try
        {
            var json = File.ReadAllText(_configPath);
            return JsonSerializer.Deserialize<DnsConfig>(json,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? new DnsConfig();
        }
        catch (Exception ex)
        {
            _logger.LogWarning(ex, "Failed to read dns.json from {Path}", _configPath);
            return new DnsConfig();
        }
    }

    private void WriteConfig(DnsConfig config)
    {
        var dir = Path.GetDirectoryName(_configPath);
        if (!string.IsNullOrEmpty(dir))
            Directory.CreateDirectory(dir);

        config.LastUpdate = DateTime.UtcNow.ToString("O");
        var json = JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(_configPath, json);
    }
}

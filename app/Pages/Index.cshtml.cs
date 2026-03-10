using AzDdns.Models;
using AzDdns.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;

namespace AzDdns.Pages;

public class IndexModel : PageModel
{
    private readonly DnsCache _cache;
    private readonly DnsConfigStore _configStore;
    private readonly AzureDnsService _dnsService;
    private readonly IConfiguration _configuration;

    public bool GuiAuthEnabled { get; private set; }
    public List<DomainEntry> Domains { get; private set; } = new();

    public IndexModel(
        DnsCache cache,
        DnsConfigStore configStore,
        AzureDnsService dnsService,
        IConfiguration configuration)
    {
        _cache = cache;
        _configStore = configStore;
        _dnsService = dnsService;
        _configuration = configuration;
    }

    public void OnGet()
    {
        GuiAuthEnabled = IsGuiAuthEnabled();
        LoadDomains();
    }

    public async Task<IActionResult> OnPostUpdateAsync(string? ipOverride)
    {
        GuiAuthEnabled = IsGuiAuthEnabled();

        if (GuiAuthEnabled && User.Identity?.IsAuthenticated != true)
            return Challenge();

        var ip = ipOverride?.Trim();
        if (string.IsNullOrEmpty(ip))
            ip = HttpContext.Connection.RemoteIpAddress?.ToString();

        if (string.IsNullOrEmpty(ip))
        {
            TempData["UpdateError"] = "Could not determine IP address.";
            LoadDomains();
            return Page();
        }

        var defaultTtl = int.TryParse(_configuration["DNS_TTL"], out var ttl) ? ttl : 3600;

        var domains = _configStore.GetManagedDomains();
        var updated = 0;
        var errors = new List<string>();

        foreach (var domain in domains)
        {
            try
            {
                var zone = await _dnsService.FindZoneForDomainAsync(domain);
                if (zone is null)
                {
                    errors.Add($"{domain}: no matching Azure DNS zone");
                    continue;
                }

                var existing = _cache.Get(domain);
                if (existing?.CurrentIp == ip)
                    continue;

                await _dnsService.SetARecordAsync(zone.Name, zone.ResourceGroup, ip, defaultTtl);

                _cache.Upsert(new DomainEntry
                {
                    Domain = domain,
                    CurrentIp = ip,
                    Ttl = defaultTtl,
                    LastFetched = DateTime.UtcNow,
                    LastUpdated = DateTime.UtcNow,
                    Error = null
                });
                updated++;
            }
            catch (Exception ex)
            {
                errors.Add($"{domain}: {ex.Message}");
                var existing = _cache.Get(domain);
                _cache.Upsert(new DomainEntry
                {
                    Domain = domain,
                    CurrentIp = existing?.CurrentIp,
                    Ttl = existing?.Ttl ?? defaultTtl,
                    LastFetched = existing?.LastFetched,
                    LastUpdated = existing?.LastUpdated,
                    Error = ex.Message
                });
            }
        }

        if (errors.Count > 0)
            TempData["UpdateError"] = $"Updated {updated} domain(s) with {errors.Count} error(s): {string.Join("; ", errors)}";
        else
            TempData["UpdateMessage"] = $"Successfully updated {updated} domain(s) to {ip}.";

        return RedirectToPage();
    }

    private void LoadDomains()
    {
        var managed = _configStore.GetManagedDomains();
        Domains = managed
            .Select(d => _cache.Get(d) ?? new DomainEntry { Domain = d })
            .ToList();
    }

    private bool IsGuiAuthEnabled() =>
        !string.IsNullOrWhiteSpace(_configuration["AZURE_APP_CLIENT_ID"]);
}

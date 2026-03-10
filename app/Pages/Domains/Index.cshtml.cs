using AzDdns.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;

namespace AzDdns.Pages.Domains;

public class IndexModel : PageModel
{
    private readonly AzureDnsService _dnsService;
    private readonly DnsConfigStore _configStore;
    private readonly DnsCache _cache;
    private readonly IConfiguration _configuration;

    public bool GuiAuthEnabled { get; private set; }
    public List<ZoneInfo> Zones { get; private set; } = new();
    public List<string> ManagedDomains { get; private set; } = new();
    public string? LoadError { get; private set; }

    public IndexModel(
        AzureDnsService dnsService,
        DnsConfigStore configStore,
        DnsCache cache,
        IConfiguration configuration)
    {
        _dnsService = dnsService;
        _configStore = configStore;
        _cache = cache;
        _configuration = configuration;
    }

    public async Task OnGetAsync()
    {
        GuiAuthEnabled = IsGuiAuthEnabled();
        ManagedDomains = _configStore.GetManagedDomains();

        try
        {
            Zones = await _dnsService.ListZonesAsync();
        }
        catch (Exception ex)
        {
            LoadError = ex.Message;
        }
    }

    public async Task<IActionResult> OnPostAddAsync(string domain)
    {
        if (IsGuiAuthEnabled() && User.Identity?.IsAuthenticated != true)
            return Challenge();

        if (string.IsNullOrWhiteSpace(domain))
        {
            TempData["Error"] = "Domain name cannot be empty.";
            return RedirectToPage();
        }

        await _configStore.AddDomainAsync(domain.Trim().ToLowerInvariant());
        TempData["Message"] = $"Domain '{domain}' added to managed domains.";
        return RedirectToPage();
    }

    public async Task<IActionResult> OnPostRemoveAsync(string domain)
    {
        if (IsGuiAuthEnabled() && User.Identity?.IsAuthenticated != true)
            return Challenge();

        if (string.IsNullOrWhiteSpace(domain))
        {
            TempData["Error"] = "Domain name cannot be empty.";
            return RedirectToPage();
        }

        await _configStore.RemoveDomainAsync(domain.Trim().ToLowerInvariant());
        _cache.Remove(domain.Trim().ToLowerInvariant());
        TempData["Message"] = $"Domain '{domain}' removed from managed domains.";
        return RedirectToPage();
    }

    private bool IsGuiAuthEnabled() =>
        !string.IsNullOrWhiteSpace(_configuration["AZURE_APP_CLIENT_ID"]);
}

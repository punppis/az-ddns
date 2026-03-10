using AzDdns.Models;
using AzDdns.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;

namespace AzDdns.Pages;

public class IndexModel : PageModel
{
    private readonly DnsCache _cache;
    private readonly DnsConfigStore _configStore;
    private readonly DnsUpdateService _updateService;
    private readonly IConfiguration _configuration;

    public bool GuiAuthEnabled { get; private set; }
    public List<DomainEntry> Domains { get; private set; } = new();

    public IndexModel(
        DnsCache cache,
        DnsConfigStore configStore,
        DnsUpdateService updateService,
        IConfiguration configuration)
    {
        _cache = cache;
        _configStore = configStore;
        _updateService = updateService;
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

        var (_, _, results) = await _updateService.UpdateAllAsync(ip);

        var updated = results.Count(r => r.Action == "updated");
        var errors  = results.Where(r => r.Action == "error").Select(r => $"{r.Domain}: {r.Error}").ToList();

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

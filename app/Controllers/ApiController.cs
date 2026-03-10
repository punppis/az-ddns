using System.Security.Cryptography;
using System.Text;
using AzDdns.Models;
using AzDdns.Services;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.Filters;

namespace AzDdns.Controllers;

/// <summary>
/// Checks X-DDNS-TOKEN header using constant-time comparison.
/// Falls through to Azure AD bearer if the header is absent/wrong.
/// </summary>
[AttributeUsage(AttributeTargets.Class | AttributeTargets.Method)]
public class DdnsTokenAttribute : ActionFilterAttribute
{
    public override void OnActionExecuting(ActionExecutingContext context)
    {
        var config = context.HttpContext.RequestServices.GetRequiredService<IConfiguration>();
        var expectedToken = config["DDNS_TOKEN"] ?? string.Empty;

        var headers = context.HttpContext.Request.Headers;
        var providedToken = headers["X-DDNS-TOKEN"].FirstOrDefault() ?? string.Empty;

        var expectedBytes = Encoding.UTF8.GetBytes(expectedToken);
        var providedBytes = Encoding.UTF8.GetBytes(providedToken);

        // Pad to equal length so FixedTimeEquals can run without throwing on length mismatch
        if (providedBytes.Length != expectedBytes.Length)
        {
            // Token length differs — still run a dummy comparison to avoid timing leaks
            CryptographicOperations.FixedTimeEquals(expectedBytes, expectedBytes);
        }
        else if (CryptographicOperations.FixedTimeEquals(providedBytes, expectedBytes) && expectedToken.Length > 0)
        {
            // Token matched — allow request
            return;
        }

        // No token match; allow Bearer passthrough only when Azure AD OIDC is actually configured
        var guiClientId = config["AZURE_APP_CLIENT_ID"] ?? string.Empty;
        if (!string.IsNullOrWhiteSpace(guiClientId) &&
            headers.ContainsKey("Authorization") &&
            (headers["Authorization"].FirstOrDefault() ?? string.Empty).StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        context.Result = new UnauthorizedObjectResult(new { error = "Unauthorized: provide X-DDNS-TOKEN header or a valid Bearer token." });
    }
}

[ApiController]
[Route("api")]
[DdnsToken]
public class ApiController : ControllerBase
{
    private readonly DnsCache _cache;
    private readonly DnsConfigStore _configStore;
    private readonly AzureDnsService _dnsService;
    private readonly IConfiguration _configuration;
    private readonly ILogger<ApiController> _logger;

    public ApiController(
        DnsCache cache,
        DnsConfigStore configStore,
        AzureDnsService dnsService,
        IConfiguration configuration,
        ILogger<ApiController> logger)
    {
        _cache = cache;
        _configStore = configStore;
        _dnsService = dnsService;
        _configuration = configuration;
        _logger = logger;
    }

    [HttpGet("list")]
    public IActionResult List()
    {
        var entries = _cache.GetAll().Select(e => new DomainInfo
        {
            Domain = e.Domain,
            CurrentIp = e.CurrentIp,
            Ttl = e.Ttl,
            LastFetched = e.LastFetched,
            LastUpdated = e.LastUpdated,
            Error = e.Error
        }).ToList();

        return Ok(new ListResponse { Success = true, Domains = entries });
    }

    [HttpPost("update")]
    public async Task<IActionResult> Update([FromBody] UpdateRequest? body)
    {
        var ip = body?.Ip;
        if (string.IsNullOrWhiteSpace(ip))
        {
            ip = HttpContext.Connection.RemoteIpAddress?.ToString();
            if (string.IsNullOrWhiteSpace(ip))
                return BadRequest(new { error = "Could not determine caller IP and none was provided." });
        }

        var defaultTtl = int.TryParse(_configuration["DNS_TTL"], out var ttl) ? ttl : 3600;

        var domains = _configStore.GetManagedDomains();
        var results = new List<DomainUpdateResult>();

        foreach (var domain in domains)
        {
            try
            {
                var zone = await _dnsService.FindZoneForDomainAsync(domain);
                if (zone is null)
                {
                    results.Add(new DomainUpdateResult
                    {
                        Domain = domain,
                        Action = "error",
                        Error = "No matching Azure DNS zone found"
                    });
                    continue;
                }

                var existing = _cache.Get(domain);
                if (existing?.CurrentIp == ip)
                {
                    results.Add(new DomainUpdateResult { Domain = domain, Action = "unchanged", NewIp = ip });
                    continue;
                }

                await _dnsService.SetARecordAsync(zone.Name, zone.ResourceGroup, ip, defaultTtl);

                _cache.Upsert(new Models.DomainEntry
                {
                    Domain = domain,
                    CurrentIp = ip,
                    Ttl = defaultTtl,
                    LastFetched = DateTime.UtcNow,
                    LastUpdated = DateTime.UtcNow,
                    Error = null
                });

                results.Add(new DomainUpdateResult { Domain = domain, Action = "updated", NewIp = ip });
                _logger.LogInformation("Updated {Domain} → {Ip}", domain, ip);
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to update {Domain}", domain);
                results.Add(new DomainUpdateResult { Domain = domain, Action = "error", Error = ex.Message });
            }
        }

        return Ok(new UpdateResponse { Success = true, DetectedIp = ip, Results = results });
    }
}

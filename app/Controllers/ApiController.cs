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

        // Pad both arrays to the same length so FixedTimeEquals always runs in
        // time proportional to the *expected* token, not the provided one.
        byte[] a = expectedBytes;
        byte[] b;
        if (providedBytes.Length == expectedBytes.Length)
        {
            b = providedBytes;
        }
        else
        {
            // Create a copy padded/trimmed to the expected length so we always
            // compare exactly expectedBytes.Length bytes.
            b = new byte[expectedBytes.Length];
            var copyLen = Math.Min(providedBytes.Length, expectedBytes.Length);
            Buffer.BlockCopy(providedBytes, 0, b, 0, copyLen);
        }

        if (CryptographicOperations.FixedTimeEquals(a, b) && expectedToken.Length > 0)
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
    private readonly DnsUpdateService _updateService;
    private readonly ILogger<ApiController> _logger;

    public ApiController(
        DnsCache cache,
        DnsUpdateService updateService,
        ILogger<ApiController> logger)
    {
        _cache = cache;
        _updateService = updateService;
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

        var (success, detectedIp, results) = await _updateService.UpdateAllAsync(ip);
        return Ok(new UpdateResponse { Success = success, DetectedIp = detectedIp, Results = results });
    }
}

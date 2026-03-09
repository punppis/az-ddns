using AzDdns.Models;
using AzDdns.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using System;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;

namespace AzDdns.Functions;

/// <summary>
/// POST /api/update
///
/// Updates Azure DNS records to the caller's current IP (or an explicit IP).
/// Requires the <c>X-DDNS-TOKEN</c> header to match <c>DDNS_TOKEN</c> in configuration.
///
/// Request body (JSON):
/// <code>
/// {
///   "ip": "1.2.3.4",           // optional — omit to use detected client IP
///   "ttl": 3600,               // optional — override server default TTL
///   "mxPreference": 10,        // optional — override server default MX preference
///   "records": {
///     "office.example.com": {
///       "A":     { "@": "{{IP}}" },
///       "CNAME": { "*": "{{DOMAIN}}" }
///     }
///   }
/// }
/// </code>
/// </summary>
public sealed class UpdateFunction(
    IConfiguration cfg,
    AzureDnsService dns,
    IpService ipSvc,
    ConcurrencyLimiter limiter,
    ILogger<UpdateFunction> logger)
{
    private static readonly JsonSerializerOptions _jsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    [Function("Update")]
    public async Task<IActionResult> RunAsync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "post", Route = "update")] HttpRequest req)
    {
        // --- 1. Token auth ------------------------------------------------
        if (!IsAuthorized(req))
        {
            logger.LogWarning("Unauthorized update attempt from {Ip}",
                req.HttpContext.Connection.RemoteIpAddress);
            return new UnauthorizedResult();
        }

        // --- 2. Parse body ------------------------------------------------
        UpdateRequest updateReq;
        try
        {
            updateReq = await JsonSerializer.DeserializeAsync<UpdateRequest>(
                req.Body, _jsonOpts) ?? new UpdateRequest();
        }
        catch (JsonException ex)
        {
            logger.LogWarning(ex, "Invalid JSON in request body.");
            return new BadRequestObjectResult(new { error = "Invalid JSON body.", detail = ex.Message });
        }

        if (updateReq.Records is null || updateReq.Records.Count == 0)
            return new BadRequestObjectResult(new { error = "Request body must contain at least one record in 'records'." });

        // --- 3. Resolve IP -----------------------------------------------
        string clientIp;
        try
        {
            clientIp = ipSvc.Resolve(req, updateReq.Ip);
        }
        catch (InvalidOperationException ex)
        {
            return new BadRequestObjectResult(new { error = ex.Message });
        }

        logger.LogInformation("Update request from IP {ClientIp} for {Count} domain(s).",
            clientIp, updateReq.Records.Count);

        // --- 4. Concurrency gate -----------------------------------------
        var response = new UpdateResponse { DetectedIp = clientIp };
        bool acquired = await limiter.TryRunExclusiveAsync(async () =>
        {
            await ProcessUpdateAsync(updateReq, clientIp, response);
        }, req.HttpContext.RequestAborted);

        if (!acquired)
        {
            logger.LogWarning("Update request rejected: another update is in progress.");
            return new ObjectResult(new { error = "Another update is currently in progress. Please retry shortly." })
            {
                StatusCode = StatusCodes.Status429TooManyRequests
            };
        }

        // --- 5. Build summary --------------------------------------------
        response.UpdatedCount   = response.Domains.SelectMany(d => d.Records).Count(r => r.Action == "updated");
        response.UnchangedCount = response.Domains.SelectMany(d => d.Records).Count(r => r.Action == "unchanged");
        response.ErrorCount     = response.Domains.Count(d => d.Error is not null)
                                + response.Domains.SelectMany(d => d.Records).Count(r => r.Action == "error");
        response.Success        = response.ErrorCount == 0;
        response.Summary = $"IP={clientIp} updated={response.UpdatedCount} unchanged={response.UnchangedCount} errors={response.ErrorCount}";

        logger.LogInformation("Update complete. {Summary}", response.Summary);
        return new OkObjectResult(response);
    }

    private async Task ProcessUpdateAsync(UpdateRequest req, string ip, UpdateResponse response)
    {
        int ttl = req.Ttl ?? (cfg.GetValue<int?>("DNS_TTL") ?? 3600);
        int mxPref = req.MxPreference ?? (cfg.GetValue<int?>("DNS_MX_PREFERENCE") ?? 10);

        foreach (var (domain, domainCfg) in req.Records)
        {
            logger.LogInformation("Processing domain: {Domain}", domain);
            var domainResult = await dns.UpdateDomainAsync(domain, domainCfg, ip, ttl, mxPref);
            response.Domains.Add(domainResult);
        }
    }

    private bool IsAuthorized(HttpRequest req)
    {
        var token = cfg["DDNS_TOKEN"];
        if (string.IsNullOrWhiteSpace(token))
        {
            logger.LogError("DDNS_TOKEN is not configured. All requests will be rejected.");
            return false;
        }
        var provided = req.Headers["X-DDNS-TOKEN"].FirstOrDefault();
        return string.Equals(provided, token, StringComparison.Ordinal);
    }
}

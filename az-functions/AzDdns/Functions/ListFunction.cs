using AzDdns.Models;
using AzDdns.Services;
using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using System;
using System.Linq;
using System.Threading.Tasks;

namespace AzDdns.Functions;

/// <summary>
/// GET /api/list
///
/// Lists live DNS records (A, CNAME, MX) for all Azure DNS zones accessible
/// to the service principal.
/// Requires the <c>X-DDNS-TOKEN</c> header.
///
/// Optional query parameter: <c>domain=office.example.com</c> to filter to a
/// specific domain / zone.
/// </summary>
public sealed class ListFunction(
    IConfiguration cfg,
    AzureDnsService dns,
    ILogger<ListFunction> logger)
{
    [Function("List")]
    public async Task<IActionResult> RunAsync(
        [HttpTrigger(AuthorizationLevel.Anonymous, "get", Route = "list")] HttpRequest req)
    {
        // --- 1. Token auth ------------------------------------------------
        if (!IsAuthorized(req))
        {
            logger.LogWarning("Unauthorized list attempt from {Ip}",
                req.HttpContext.Connection.RemoteIpAddress);
            return new UnauthorizedResult();
        }

        // --- 2. Optional domain filter -----------------------------------
        var domainFilter = req.Query["domain"].FirstOrDefault();

        logger.LogInformation("List request{Filter}.",
            domainFilter is not null ? $" filtered to domain '{domainFilter}'" : "");

        // --- 3. Fetch zones ----------------------------------------------
        var response = new ListResponse();
        try
        {
            var zones = await dns.ListZonesAsync();

            if (domainFilter is not null)
            {
                var zoneMatch = await dns.FindZoneForDomainAsync(domainFilter);
                if (zoneMatch is null)
                {
                    response.Success = false;
                    response.Summary = $"No DNS zone found for domain '{domainFilter}'.";
                    return new NotFoundObjectResult(response);
                }
                zones = zones.Where(z =>
                    z.ZoneName.Equals(zoneMatch.Value.ZoneName, StringComparison.OrdinalIgnoreCase)).ToList();
            }

            foreach (var (zoneName, rg, _) in zones)
            {
                var zoneInfo = new ZoneInfo
                {
                    Domain = zoneName,
                    Zone = zoneName,
                    ResourceGroup = rg
                };
                try
                {
                    zoneInfo.Records = await dns.ListRecordsAsync(zoneName, rg);
                }
                catch (Exception ex)
                {
                    logger.LogError(ex, "Failed to list records for zone {Zone}.", zoneName);
                    zoneInfo.Error = ex.Message;
                }
                response.Zones.Add(zoneInfo);
            }

            response.Success = true;
            response.Summary = $"Listed {response.Zones.Count} zone(s), " +
                               $"{response.Zones.Sum(z => z.Records.Count)} record(s).";
        }
        catch (Exception ex)
        {
            logger.LogError(ex, "List operation failed.");
            response.Success = false;
            response.Summary = ex.Message;
            return new ObjectResult(response) { StatusCode = StatusCodes.Status500InternalServerError };
        }

        logger.LogInformation("List complete. {Summary}", response.Summary);
        return new OkObjectResult(response);
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

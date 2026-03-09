using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;
using System.Net;
using System.Net.Http;
using System.Text.RegularExpressions;

namespace AzDdns.Services;

/// <summary>
/// Resolves the caller's public IP from the HTTP request.
/// Priority order:
///   1. Explicit IP provided by caller in the request body.
///   2. X-Forwarded-For header (leftmost entry — the original client IP).
///   3. REMOTE_ADDR / HttpContext.Connection.RemoteIpAddress.
/// </summary>
public sealed class IpService(ILogger<IpService> logger)
{
    private static readonly Regex _ipv4 =
        new(@"^(?:\d{1,3}\.){3}\d{1,3}$", RegexOptions.Compiled);

    /// <summary>Returns true if <paramref name="s"/> is a valid IPv4 address string.</summary>
    public static bool IsIPv4(string? s)
    {
        if (string.IsNullOrWhiteSpace(s)) return false;
        if (!_ipv4.IsMatch(s.Trim())) return false;
        return IPAddress.TryParse(s.Trim(), out var addr) &&
               addr.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork;
    }

    /// <summary>
    /// Returns the best IP for this request.
    /// <paramref name="explicitIp"/> takes precedence when it is a valid IPv4 address.
    /// </summary>
    public string Resolve(HttpRequest request, string? explicitIp)
    {
        // 1. Caller-supplied explicit IP
        if (!string.IsNullOrWhiteSpace(explicitIp))
        {
            if (IsIPv4(explicitIp))
            {
                logger.LogInformation("Using caller-supplied IP: {Ip}", explicitIp);
                return explicitIp.Trim();
            }
            logger.LogWarning("Caller-supplied IP {Ip} is not valid; falling back to detected IP.", explicitIp);
        }

        // 2. X-Forwarded-For (leftmost = original client when behind a load-balancer)
        var xff = request.Headers["X-Forwarded-For"].FirstOrDefault();
        if (!string.IsNullOrWhiteSpace(xff))
        {
            var first = xff.Split(',')[0].Trim();
            if (IsIPv4(first))
            {
                logger.LogInformation("Detected client IP via X-Forwarded-For: {Ip}", first);
                return first;
            }
        }

        // 3. Direct connection IP
        var remoteIp = request.HttpContext.Connection.RemoteIpAddress;
        if (remoteIp is not null)
        {
            // If IPv4-mapped IPv6 (::ffff:1.2.3.4), unwrap it
            if (remoteIp.IsIPv4MappedToIPv6)
                remoteIp = remoteIp.MapToIPv4();

            var ipStr = remoteIp.ToString();
            if (IsIPv4(ipStr))
            {
                logger.LogInformation("Detected client IP via RemoteIpAddress: {Ip}", ipStr);
                return ipStr;
            }
        }

        throw new InvalidOperationException(
            "Unable to determine a valid IPv4 address for this request. " +
            "Pass an explicit 'ip' field in the request body.");
    }
}

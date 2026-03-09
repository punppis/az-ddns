using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;
using StackExchange.Redis;
using System;
using System.Threading;
using System.Threading.Tasks;

namespace AzDdns.Services;

/// <summary>
/// Ensures at most one DDNS update runs at a time.
///
/// When a Redis connection string is configured the lock is distributed
/// (safe for multiple function instances / slots). Without Redis an
/// in-process <see cref="SemaphoreSlim"/> is used instead.
/// </summary>
public sealed class ConcurrencyLimiter : IAsyncDisposable
{
    private const string RedisLockKey = "DNS_ddns:update-lock";

    private readonly ILogger<ConcurrencyLimiter> _logger;
    private readonly TimeSpan _lockTimeout;
    private readonly SemaphoreSlim _localSem = new(1, 1);
    private IConnectionMultiplexer? _redis;
    private readonly string _instanceId = Guid.NewGuid().ToString("N");

    public ConcurrencyLimiter(IConfiguration cfg, ILogger<ConcurrencyLimiter> logger)
    {
        _logger = logger;
        var timeoutSec = cfg.GetValue<int?>("DDNS_CONCURRENCY_TIMEOUT_SECONDS") ?? 30;
        _lockTimeout = TimeSpan.FromSeconds(timeoutSec);

        var connStr = cfg["REDIS_CONNECTION_STRING"];
        if (!string.IsNullOrWhiteSpace(connStr))
        {
            try
            {
                _redis = ConnectionMultiplexer.Connect(connStr);
                _logger.LogInformation("ConcurrencyLimiter: using Redis distributed lock.");
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "ConcurrencyLimiter: Redis connect failed; falling back to in-process semaphore.");
                _redis = null;
            }
        }
        else
        {
            _logger.LogInformation("ConcurrencyLimiter: no Redis configured; using in-process semaphore.");
        }
    }

    /// <summary>
    /// Tries to acquire the concurrency lock and run <paramref name="work"/>.
    /// Returns false if the lock cannot be acquired within the configured timeout.
    /// </summary>
    public async Task<bool> TryRunExclusiveAsync(Func<Task> work, CancellationToken ct = default)
    {
        if (_redis is not null)
            return await TryRunWithRedisLockAsync(work, ct);

        return await TryRunWithLocalSemAsync(work, ct);
    }

    private async Task<bool> TryRunWithLocalSemAsync(Func<Task> work, CancellationToken ct)
    {
        if (!await _localSem.WaitAsync(_lockTimeout, ct))
        {
            _logger.LogWarning("Concurrency limit reached; request rejected.");
            return false;
        }
        try
        {
            await work();
            return true;
        }
        finally
        {
            _localSem.Release();
        }
    }

    private async Task<bool> TryRunWithRedisLockAsync(Func<Task> work, CancellationToken ct)
    {
        var db = _redis!.GetDatabase();
        // Attempt to acquire lock with TTL slightly longer than the timeout so
        // the lock expires even if the process crashes mid-operation.
        var lockTtl = _lockTimeout + TimeSpan.FromSeconds(10);
        var acquired = await db.StringSetAsync(
            RedisLockKey, _instanceId,
            lockTtl, When.NotExists);

        if (!acquired)
        {
            _logger.LogWarning("Concurrency limit reached (Redis lock held); request rejected.");
            return false;
        }

        try
        {
            await work();
            return true;
        }
        finally
        {
            // Only release our own lock
            var current = await db.StringGetAsync(RedisLockKey);
            if (current == _instanceId)
                await db.KeyDeleteAsync(RedisLockKey);
        }
    }

    public async ValueTask DisposeAsync()
    {
        _localSem.Dispose();
        if (_redis is IAsyncDisposable ad)
            await ad.DisposeAsync();
        else
            _redis?.Dispose();
    }
}

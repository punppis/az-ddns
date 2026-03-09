using AzDdns.Services;
using Microsoft.Azure.Functions.Worker;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

var host = new HostBuilder()
    .ConfigureFunctionsWebApplication()
    .ConfigureServices((ctx, services) =>
    {
        services.ConfigureFunctionsApplicationInsights();

        // Core services
        services.AddSingleton<AzureDnsService>();
        services.AddSingleton<IpService>();
        services.AddSingleton<ConcurrencyLimiter>();
    })
    .Build();

// Run RBAC check at startup (logs warning if role is missing; does not block)
using (var scope = host.Services.CreateScope())
{
    var dns = scope.ServiceProvider.GetRequiredService<AzureDnsService>();
    try
    {
        await dns.VerifyRolesAsync();
    }
    catch
    {
        // VerifyRolesAsync already logs; swallow here so startup is never blocked
    }
}

await host.RunAsync();
